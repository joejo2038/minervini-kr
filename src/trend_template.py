"""미너비니 Trend Template - 1단계 필터.

여덟 개 조건을 전 종목에 대해 한 번에 벡터 판정합니다.
조건별 통과 여부를 보존하기 때문에, 어떤 종목이 어디서 걸렸는지
프론트에서 그대로 보여줄 수 있습니다.
"""
from __future__ import annotations

import pandas as pd

from . import indicators as ind

CONDITION_LABELS = {
    "c1": "종가 > 150일선 & 200일선",
    "c2": "150일선 > 200일선",
    "c3": "200일선 상승 추세",
    "c4": "50일선 > 150일선 > 200일선",
    "c5": "종가 > 50일선",
    "c6": "52주 최저 대비 +30% 이상",
    "c7": "52주 최고 대비 -25% 이내",
    "c8": "RS Rating 70 이상",
}


def evaluate(close_w: pd.DataFrame, high_w: pd.DataFrame, low_w: pd.DataFrame,
             rs: pd.Series, cfg) -> pd.DataFrame:
    """
    Returns
    -------
    DataFrame indexed by code with columns:
        c1..c8 (bool), passed (int), tt_pass (bool),
        close, ma50, ma150, ma200, hi52, lo52,
        from_low_pct, from_high_pct, rs_rating
    """
    n_short = cfg.get("trend_template.ma_short", 50)
    n_mid = cfg.get("trend_template.ma_mid", 150)
    n_long = cfg.get("trend_template.ma_long", 200)

    mas = ind.moving_averages(close_w, [n_short, n_mid, n_long])
    ma_s, ma_m, ma_l = mas[n_short], mas[n_mid], mas[n_long]
    hi52, lo52 = ind.rolling_extremes(high_w, low_w, 252)

    close = close_w.iloc[-1]
    s, m, l = ma_s.iloc[-1], ma_m.iloc[-1], ma_l.iloc[-1]
    hi, lo = hi52.iloc[-1], lo52.iloc[-1]

    c1 = (close > m) & (close > l)
    c2 = m > l
    c3 = ind.ma_rising(ma_l,
                       cfg.get("trend_template.ma_long_rising_days", 22),
                       cfg.get("trend_template.ma_long_rising_ratio", 0.6))
    c4 = (s > m) & (m > l)
    c5 = close > s
    c6 = close >= lo * (1.0 + cfg.get("trend_template.min_above_52w_low", 0.30))
    c7 = close >= hi * (1.0 - cfg.get("trend_template.max_below_52w_high", 0.25))
    c8 = rs >= cfg.get("trend_template.min_rs_rating", 70)

    flags = pd.DataFrame({
        "c1": c1, "c2": c2, "c3": c3, "c4": c4,
        "c5": c5, "c6": c6, "c7": c7, "c8": c8,
    }).reindex(close_w.columns)
    flags = flags.fillna(False).astype(bool)

    required = cfg.get("trend_template.required_conditions", 8)
    out = flags.copy()
    out["passed"] = flags.sum(axis=1).astype(int)
    out["tt_pass"] = out["passed"] >= required

    out["close"] = close
    out["ma50"] = s
    out["ma150"] = m
    out["ma200"] = l
    out["hi52"] = hi
    out["lo52"] = lo
    out["from_low_pct"] = (close / lo - 1.0) * 100.0
    out["from_high_pct"] = (close / hi - 1.0) * 100.0
    out["rs_rating"] = rs.reindex(out.index)

    # MA200 데이터가 없는 신규 상장주는 조건 자체가 성립하지 않습니다.
    out.loc[l.isna(), "tt_pass"] = False
    return out
