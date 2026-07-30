"""
시장 내부 지표 (market breadth / internals).

지수는 대형주 몇 개로 버틸 수 있지만 시장 폭은 못 속입니다. 개별 종목의
건강 상태를 집계해 시장 전체의 국면을 봅니다. 미너비니가 국면 판정에
지수보다 시장 폭을 앞세우는 이유입니다.

네 지표를 서로 다른 시간축에서 봅니다.
  200일선 위 비율  — 장기 추세. 바닥과 천장을 크게 잡습니다.
  50일선 위 비율   — 단기 추세. 반등의 진위를 봅니다.
  신고가-신저가     — 모멘텀 극단. 순매수 압력의 방향입니다.
  1단계 통과 수     — 전략 관점. 실제로 살 만한 종목이 얼마나 있는가.

전부 과거로 소급 계산합니다. 매일 기록을 쌓을 필요 없이 이미 있는
일봉만으로 지난 수백일 추이를 지금 당장 그립니다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import indicators as ind

log = logging.getLogger(__name__)


def compute_series(close_w: pd.DataFrame, high_w: pd.DataFrame,
                   low_w: pd.DataFrame, rs_history: pd.DataFrame | None,
                   cfg, tail_days: int = 900) -> dict:
    """
    시장 내부 지표 시계열을 계산합니다.

    Parameters
    ----------
    close_w, high_w, low_w : [날짜 x 종목] wide 패널
    rs_history : [날짜 x 종목] RS Rating 패널. None이면 1단계 통과 수를 생략합니다.
    tail_days : 결과로 돌려줄 최근 거래일 수. 계산은 전체로 하고 출력만 자릅니다.

    Returns
    -------
    dict: dates + 각 지표 시계열 + 최신 요약
    """
    n_short = cfg.get("regime.ma_short", 50)
    n_long = cfg.get("regime.ma_long", 200)

    ma_s = close_w.rolling(n_short, min_periods=n_short).mean()
    ma_l = close_w.rolling(n_long, min_periods=n_long).mean()

    # --- 이동평균 위 비율 ---
    # 각 날짜마다 (종가 > 이평) 인 종목 / 이평이 유효한 종목
    above_s = _pct_above(close_w, ma_s)
    above_l = _pct_above(close_w, ma_l)

    # --- 신고가 - 신저가 ---
    # 52주 신고가 종목 수에서 신저가 종목 수를 뺀 순값. 순매수 압력의 방향.
    hi52 = high_w.rolling(252, min_periods=200).max()
    lo52 = low_w.rolling(252, min_periods=200).min()
    at_high = (close_w >= hi52 * 0.999) & hi52.notna()
    at_low = (close_w <= lo52 * 1.001) & lo52.notna()
    listed = close_w.notna()
    n_high = at_high.sum(axis=1)
    n_low = at_low.sum(axis=1)
    n_listed = listed.sum(axis=1).replace(0, np.nan)
    # 절대 수와 순비율 둘 다 제공
    nh_nl = n_high - n_low
    nh_nl_pct = ((n_high - n_low) / n_listed * 100)

    # --- 1단계(Trend Template) 통과 수 ---
    tt_count = None
    if rs_history is not None:
        tt_count = _trend_template_count(close_w, high_w, low_w, ma_s, ma_l,
                                         hi52, lo52, rs_history, cfg)

    # --- 지수(참고선) ---
    breadth_ma = above_l.rolling(20, min_periods=5).mean()  # 20일 평활선

    dates = close_w.index
    tail = min(tail_days, len(dates))
    sl = slice(len(dates) - tail, len(dates))
    d_out = [d.strftime("%Y-%m-%d") for d in dates[sl]]

    def arr(series, nd=1):
        return [None if pd.isna(v) else round(float(v), nd) for v in series.iloc[sl]]

    series = {
        "dates": d_out,
        "above_ma200": arr(above_l),
        "above_ma200_smooth": arr(breadth_ma),
        "above_ma50": arr(above_s),
        "nh_nl": [None if pd.isna(v) else int(v) for v in nh_nl.iloc[sl]],
        "nh_nl_pct": arr(nh_nl_pct, 2),
    }
    if tt_count is not None:
        series["tt_count"] = [None if pd.isna(v) else int(v) for v in tt_count.iloc[sl]]

    # --- 국면 밴드 임계선 ---
    caution = cfg.get("regime.breadth_caution", 0.40) * 100
    risk_off = cfg.get("regime.breadth_risk_off", 0.25) * 100

    # --- 최신 요약 + 추세 방향 ---
    summary = _summary(above_l, above_s, nh_nl, tt_count, caution, risk_off)

    return {
        "series": series,
        "thresholds": {"caution": round(caution, 1), "risk_off": round(risk_off, 1)},
        "summary": summary,
    }


def _pct_above(close_w: pd.DataFrame, ma: pd.DataFrame) -> pd.Series:
    valid = ma.notna() & close_w.notna()
    above = (close_w > ma) & valid
    denom = valid.sum(axis=1).replace(0, np.nan)
    return (above.sum(axis=1) / denom * 100)


def _trend_template_count(close_w, high_w, low_w, ma_s, ma_l,
                          hi52, lo52, rs_history, cfg) -> pd.Series:
    """
    매 거래일 Trend Template 8조건을 통과한 종목 수.

    150일선이 추가로 필요하므로 여기서 계산합니다. 조건 3(200일선 상승
    추세)은 벡터로 근사합니다.
    """
    n_mid = cfg.get("trend_template.ma_mid", 150)
    ma_m = close_w.rolling(n_mid, min_periods=n_mid).mean()

    d = cfg.get("trend_template.ma_long_rising_days", 22)
    ratio = cfg.get("trend_template.ma_long_rising_ratio", 0.6)
    direction = ma_l > ma_l.shift(d)
    up_ratio = (ma_l.diff() > 0).rolling(d, min_periods=d).sum() / d
    c3 = direction & (up_ratio >= ratio)

    min_low = 1.0 + cfg.get("trend_template.min_above_52w_low", 0.30)
    max_high = 1.0 - cfg.get("trend_template.max_below_52w_high", 0.25)
    min_rs = cfg.get("trend_template.min_rs_rating", 70)
    required = cfg.get("trend_template.required_conditions", 8)

    c = close_w
    flags = [
        (c > ma_m) & (c > ma_l),
        ma_m > ma_l,
        c3,
        (ma_s > ma_m) & (ma_m > ma_l),
        c > ma_s,
        c >= lo52 * min_low,
        c >= hi52 * max_high,
        rs_history >= min_rs,
    ]
    passed = sum(f.fillna(False).astype(int) for f in flags)
    tt = (passed >= required) & ma_l.notna()
    return tt.sum(axis=1)


def _summary(above_l, above_s, nh_nl, tt_count, caution, risk_off) -> dict:
    """최신값과 20일 전 대비 방향을 요약합니다."""
    def latest(series):
        s = series.dropna()
        return float(s.iloc[-1]) if len(s) else None

    def trend(series, window=20):
        s = series.dropna()
        if len(s) < window + 1:
            return None
        now, past = float(s.iloc[-1]), float(s.iloc[-window - 1])
        return round(now - past, 1)

    cur = latest(above_l)
    if cur is None:
        phase = "unknown"
    elif cur < risk_off:
        phase = "risk_off"
    elif cur < caution:
        phase = "caution"
    else:
        phase = "healthy"

    return {
        "above_ma200": _r(latest(above_l)),
        "above_ma200_20d": trend(above_l),
        "above_ma50": _r(latest(above_s)),
        "above_ma50_20d": trend(above_s),
        "nh_nl": int(nh_nl.dropna().iloc[-1]) if len(nh_nl.dropna()) else None,
        "tt_count": int(tt_count.dropna().iloc[-1]) if tt_count is not None and len(tt_count.dropna()) else None,
        "tt_count_20d": trend(tt_count) if tt_count is not None else None,
        "phase": phase,
    }


def _r(v, nd=1):
    return None if v is None else round(v, nd)
