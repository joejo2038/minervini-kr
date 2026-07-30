"""시장 국면 판정.

미너비니가 반복해서 강조하는 지점입니다. 아무리 좋은 셋업도 시장이
조정 국면이면 실패율이 급등합니다. 개별 종목 신호보다 이걸 먼저 봐야 합니다.

두 축으로 봅니다.
  1) 지수 자체의 추세 (KOSPI / KOSDAQ 대 50일선, 200일선)
  2) 시장 폭 (전 종목 중 200일선 위에 있는 비율)

지수는 대형주 몇 개로 버틸 수 있지만 시장 폭은 못 속입니다.
"""
from __future__ import annotations

import pandas as pd


def _index_state(series: pd.DataFrame, n_short: int, n_long: int) -> dict:
    if series is None or series.empty or len(series) < n_long:
        return {"available": False}

    s = series.sort_values("date")
    close = pd.to_numeric(s["close"], errors="coerce")
    ma_s = close.rolling(n_short, min_periods=n_short).mean()
    ma_l = close.rolling(n_long, min_periods=n_long).mean()

    c = float(close.iloc[-1])
    m_s = float(ma_s.iloc[-1]) if pd.notna(ma_s.iloc[-1]) else float("nan")
    m_l = float(ma_l.iloc[-1]) if pd.notna(ma_l.iloc[-1]) else float("nan")

    above_short = c > m_s if pd.notna(m_s) else False
    above_long = c > m_l if pd.notna(m_l) else False
    rising_long = (pd.notna(ma_l.iloc[-1]) and len(ma_l) > 22
                   and pd.notna(ma_l.iloc[-23])
                   and ma_l.iloc[-1] > ma_l.iloc[-23])

    if above_short and above_long and rising_long:
        state = "uptrend"
    elif above_long:
        state = "neutral"
    else:
        state = "downtrend"

    return {
        "available": True,
        "close": round(c, 2),
        "ma_short": round(m_s, 2) if pd.notna(m_s) else None,
        "ma_long": round(m_l, 2) if pd.notna(m_l) else None,
        "above_short": bool(above_short),
        "above_long": bool(above_long),
        "rising_long": bool(rising_long),
        "state": state,
        "pct_from_ma_long": round((c / m_l - 1) * 100, 2) if pd.notna(m_l) and m_l else None,
    }


def evaluate(store, breadth_above_ma200: float, tt_pass_count: int,
             universe_size: int, cfg) -> dict:
    """
    Parameters
    ----------
    breadth_above_ma200 : 0~1. 전 종목 중 200일선 위 비율.
    """
    n_short = cfg.get("regime.ma_short", 50)
    n_long = cfg.get("regime.ma_long", 200)

    kospi = _index_state(store.index_series(cfg.get("regime.kospi_code", "1001")),
                         n_short, n_long)
    kosdaq = _index_state(store.index_series(cfg.get("regime.kosdaq_code", "2001")),
                          n_short, n_long)

    caution = cfg.get("regime.breadth_caution", 0.40)
    risk_off = cfg.get("regime.breadth_risk_off", 0.25)

    states = [x.get("state") for x in (kospi, kosdaq) if x.get("available")]
    n_up = states.count("uptrend")
    n_down = states.count("downtrend")

    if breadth_above_ma200 < risk_off or n_down == len(states) and states:
        verdict, note = "risk_off", "관망. 신규 진입을 멈추고 현금 비중을 올릴 구간입니다."
    elif breadth_above_ma200 < caution or n_down > 0:
        verdict, note = "caution", "선별 진입. 포지션 크기를 줄이고 손절을 타이트하게 잡으세요."
    elif n_up == len(states) and states:
        verdict, note = "risk_on", "정상 진입. 셋업이 나오면 계획대로 실행할 구간입니다."
    else:
        verdict, note = "neutral", "중립. 지수는 버티지만 확신 구간은 아닙니다."

    return {
        "kospi": kospi,
        "kosdaq": kosdaq,
        "breadth_above_ma200": round(breadth_above_ma200 * 100, 1),
        "tt_pass_count": int(tt_pass_count),
        "tt_pass_pct": round(tt_pass_count / universe_size * 100, 1) if universe_size else 0.0,
        "verdict": verdict,
        "note": note,
    }
