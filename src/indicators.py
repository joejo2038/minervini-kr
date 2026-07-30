"""지표 계산.

long-format 일봉을 wide(pivot)로 돌려서 벡터 연산합니다.
종목별 groupby 루프보다 10배 이상 빠릅니다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def to_wide(prices: pd.DataFrame, field: str = "close") -> pd.DataFrame:
    """index=date, columns=code 형태로 변환합니다."""
    w = prices.pivot(index="date", columns="code", values=field)
    w.index = pd.to_datetime(w.index)
    return w.sort_index()


def moving_averages(close_w: pd.DataFrame, windows: list[int]) -> dict[int, pd.DataFrame]:
    return {n: close_w.rolling(n, min_periods=n).mean() for n in windows}


def rolling_extremes(high_w: pd.DataFrame, low_w: pd.DataFrame,
                     window: int = 252) -> tuple[pd.DataFrame, pd.DataFrame]:
    hi = high_w.rolling(window, min_periods=int(window * 0.8)).max()
    lo = low_w.rolling(window, min_periods=int(window * 0.8)).min()
    return hi, lo


def ma_rising(ma_w: pd.DataFrame, days: int, min_ratio: float) -> pd.Series:
    """
    MA가 최근 `days` 거래일 동안 상승 추세인지 판정합니다.

    두 조건을 함께 봅니다.
      1) 현재 MA > days일 전 MA  (구간 전체 방향)
      2) 구간 내 전일 대비 상승한 날의 비율 >= min_ratio  (추세의 일관성)

    조건 2가 없으면 급등 후 횡보하는 종목이 통과해버립니다.
    """
    if len(ma_w) < days + 1:
        return pd.Series(False, index=ma_w.columns)

    tail = ma_w.iloc[-(days + 1):]
    direction = tail.iloc[-1] > tail.iloc[0]
    up_days = (tail.diff().iloc[1:] > 0).sum()
    consistency = (up_days / days) >= min_ratio
    return (direction & consistency).fillna(False)


def rs_raw(close_w: pd.DataFrame, periods: list[int], weights: list[float]) -> pd.Series:
    """
    IBD 방식 가중 상대강도 원점수.

        RS = Σ wᵢ × (현재가 / periodsᵢ일 전 가격)

    기본값은 3개월 40%, 6/9/12개월 각 20%입니다.
    데이터가 부족한 종목은 NaN으로 남겨 랭킹에서 자동 제외됩니다.
    """
    if len(weights) != len(periods):
        raise ValueError("periods와 weights 길이가 다릅니다")

    last = close_w.iloc[-1]
    score = pd.Series(0.0, index=close_w.columns)
    valid = pd.Series(True, index=close_w.columns)

    for period, weight in zip(periods, weights):
        if len(close_w) <= period:
            # 이 구간을 계산할 만큼 데이터가 없으면 전 종목 무효
            return pd.Series(np.nan, index=close_w.columns)
        past = close_w.iloc[-(period + 1)]
        ratio = last / past
        valid &= past.notna() & (past > 0) & last.notna()
        score += weight * ratio.fillna(0)

    return score.where(valid)


def rs_rating(raw: pd.Series, groups: pd.Series | None = None) -> pd.Series:
    """
    원점수를 1~99 백분위로 변환합니다. 이게 미너비니 조건 8의 RS Rating입니다.

    groups를 주면 그룹(시장) 안에서 랭킹합니다.
    """
    def _rank(s: pd.Series) -> pd.Series:
        v = s.dropna()
        if v.empty:
            return pd.Series(np.nan, index=s.index)
        pct = v.rank(pct=True, method="average")
        scaled = (pct * 98 + 1).round().clip(1, 99)
        return scaled.reindex(s.index)

    if groups is None:
        return _rank(raw)

    out = pd.Series(np.nan, index=raw.index)
    for g in groups.dropna().unique():
        members = groups[groups == g].index
        members = raw.index.intersection(members)
        if len(members) == 0:
            continue
        out.loc[members] = _rank(raw.loc[members])
    return out


def rs_rating_history(close_w: pd.DataFrame, periods: list[int],
                      weights: list[float]) -> pd.DataFrame:
    """
    매 거래일의 RS Rating을 [날짜 x 종목] 패널로 계산합니다.

    rs_raw는 마지막 날 한 점만 계산하지만, 시장 내부 지표는 매일의
    1단계 통과 수가 필요하므로 전 기간 버전이 따로 필요합니다.
    """
    if len(weights) != len(periods):
        raise ValueError("periods와 weights 길이가 다릅니다")

    raw = None
    valid = pd.DataFrame(True, index=close_w.index, columns=close_w.columns)
    for period, weight in zip(periods, weights):
        past = close_w.shift(period)
        valid &= past.notna() & (past > 0) & close_w.notna()
        ratio = (close_w / past) * weight
        raw = ratio if raw is None else raw + ratio

    raw = raw.where(valid)
    return raw.rank(axis=1, pct=True, method="average") * 98 + 1


def average_value(value_w: pd.DataFrame, window: int = 20) -> pd.Series:
    """20일 평균 거래대금 (원)."""
    return value_w.rolling(window, min_periods=max(5, window // 2)).mean().iloc[-1]


def atr(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 14) -> float:
    """단일 종목 ATR. VCP 리스크 산정에 씁니다."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    result = tr.rolling(window, min_periods=window).mean()
    return float(result.iloc[-1]) if not result.empty and pd.notna(result.iloc[-1]) else float("nan")


def pct(a: float, b: float) -> float:
    """(a/b - 1) * 100. 0으로 나누면 nan."""
    if b in (0, None) or pd.isna(b) or pd.isna(a):
        return float("nan")
    return (a / b - 1.0) * 100.0
