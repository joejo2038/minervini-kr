"""VCP (Volatility Contraction Pattern) 탐지.

Trend Template이 후보군을 뽑는다면, VCP는 진입 타이밍을 잡습니다.
핵심 아이디어는 단순합니다. 베이스를 만드는 동안 조정폭이 계단식으로
줄어들고(T1 > T2 > T3), 거래량도 함께 말라야 한다는 것입니다.
그 끝에서 매물이 소진되면 적은 거래량으로도 저항을 뚫습니다.

한국 시장 보정
--------------
상하한가 ±30% 구조 때문에 일간 변동성이 미국보다 큽니다.
스윙 판정 임계값(swing_threshold)과 첫 수축 허용 깊이(max_first_depth)를
config.yaml에서 미국 원안보다 넉넉하게 잡아두었습니다.
백테스트 후 반드시 재조정하세요.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd


# --------------------------------------------------------------------
# 지그재그 스윙 추출
# --------------------------------------------------------------------

def zigzag(high: np.ndarray, low: np.ndarray, threshold: float) -> list[tuple[int, float, str]]:
    """
    퍼센트 반전 방식 지그재그.

    고점을 추적하다가 threshold만큼 밀리면 그 고점을 스윙 고점으로 확정하고
    방향을 뒤집습니다. 반대도 동일합니다.

    Returns
    -------
    [(index, price, 'H' | 'L'), ...] 시간순
    """
    n = len(high)
    if n < 3:
        return []

    pivots: list[tuple[int, float, str]] = []
    hi_i, hi_p = 0, float(high[0])
    lo_i, lo_p = 0, float(low[0])
    trend = 0  # 0=미정, 1=상승중(고점탐색), -1=하락중(저점탐색)

    for i in range(1, n):
        h, l = float(high[i]), float(low[i])

        if trend == 0:
            if h > hi_p:
                hi_i, hi_p = i, h
            if l < lo_p:
                lo_i, lo_p = i, l
            if h >= lo_p * (1.0 + threshold):
                pivots.append((lo_i, lo_p, "L"))
                trend, hi_i, hi_p = 1, i, h
            elif l <= hi_p * (1.0 - threshold):
                pivots.append((hi_i, hi_p, "H"))
                trend, lo_i, lo_p = -1, i, l

        elif trend == 1:
            if h > hi_p:
                hi_i, hi_p = i, h
            elif l <= hi_p * (1.0 - threshold):
                pivots.append((hi_i, hi_p, "H"))
                trend, lo_i, lo_p = -1, i, l

        else:
            if l < lo_p:
                lo_i, lo_p = i, l
            elif h >= lo_p * (1.0 + threshold):
                pivots.append((lo_i, lo_p, "L"))
                trend, hi_i, hi_p = 1, i, h

    # 마지막 미확정 극값도 잠정 스윙으로 포함합니다.
    if trend == 1:
        pivots.append((hi_i, hi_p, "H"))
    elif trend == -1:
        pivots.append((lo_i, lo_p, "L"))

    return pivots


# --------------------------------------------------------------------
# 결과 컨테이너
# --------------------------------------------------------------------

@dataclass
class Contraction:
    start_idx: int
    end_idx: int
    high: float
    low: float
    depth_pct: float      # 조정 깊이 (%)
    days: int
    avg_volume: float

    def to_dict(self) -> dict:
        return {
            "depth_pct": round(self.depth_pct, 2),
            "days": self.days,
            "high": round(self.high, 1),
            "low": round(self.low, 1),
            "avg_volume": int(self.avg_volume) if np.isfinite(self.avg_volume) else 0,
        }


@dataclass
class VCPResult:
    detected: bool = False
    score: float = 0.0
    reason: str = ""
    base_days: int = 0
    contractions: list[Contraction] = field(default_factory=list)
    pivot: float = float("nan")
    dist_to_pivot_pct: float = float("nan")
    volume_ratio: float = float("nan")     # 마지막 수축 거래량 / 첫 수축 거래량
    dryup_ratio: float = float("nan")      # 최근 5일 / 50일 평균 거래량
    breakout: bool = False
    stop_price: float = float("nan")
    risk_pct: float = float("nan")         # 진입가 대비 손절까지 거리

    def to_dict(self) -> dict:
        d = asdict(self)
        d["contractions"] = [c.to_dict() for c in self.contractions]
        for k in ("score", "pivot", "dist_to_pivot_pct", "volume_ratio",
                  "dryup_ratio", "stop_price", "risk_pct"):
            v = d.get(k)
            if v is not None and isinstance(v, float):
                d[k] = None if not np.isfinite(v) else round(v, 2)
        return d


# --------------------------------------------------------------------
# 탐지 본체
# --------------------------------------------------------------------

def detect(df: pd.DataFrame, cfg) -> VCPResult:
    """
    Parameters
    ----------
    df : 단일 종목 일봉. high, low, close, volume 컬럼 필요. 날짜 오름차순.
    """
    lookback = cfg.get("vcp.lookback_days", 120)
    if df is None or len(df) < 30:
        res = VCPResult()
        res.reason = "데이터 부족"
        return res
    w = df.tail(lookback)
    return detect_arrays(
        w["high"].to_numpy(dtype=float),
        w["low"].to_numpy(dtype=float),
        w["close"].to_numpy(dtype=float),
        w["volume"].to_numpy(dtype=float),
        cfg,
    )


def detect_arrays(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                  volume: np.ndarray, cfg) -> VCPResult:
    """
    배열을 직접 받는 탐지 본체.

    백테스트는 (날짜 x 종목) 조합마다 이 함수를 호출합니다. DataFrame을
    매번 만들면 오버헤드가 지배적이라 numpy 경로를 따로 둡니다.
    입력 배열은 이미 lookback 구간으로 잘려 있다고 가정합니다.
    """
    min_ct = cfg.get("vcp.min_contractions", 2)
    max_ct = cfg.get("vcp.max_contractions", 5)
    thr = cfg.get("vcp.swing_threshold", 0.05)
    max_first = cfg.get("vcp.max_first_depth", 0.40) * 100
    max_last = cfg.get("vcp.max_last_depth", 0.15) * 100
    tol = cfg.get("vcp.contraction_tolerance", 1.0)
    max_vol_ratio = cfg.get("vcp.max_volume_ratio", 0.80)
    max_dryup = cfg.get("vcp.max_dryup_ratio", 0.85)
    max_dist = cfg.get("vcp.max_dist_to_pivot", 0.08) * 100
    max_extended = cfg.get("vcp.max_extended_above_pivot", 0.05) * 100
    bo_mult = cfg.get("vcp.breakout_volume_mult", 1.5)
    min_base = cfg.get("vcp.min_base_days", 15)
    max_base = cfg.get("vcp.max_base_days", 120)

    res = VCPResult()

    if len(close) < max(min_base + 5, 30):
        res.reason = "데이터 부족"
        return res

    pivots = zigzag(high, low, thr)
    if len(pivots) < 3:
        res.reason = "스윙 부족"
        return res

    # 고점 → 저점 쌍을 수축 구간으로 변환
    contractions: list[Contraction] = []
    for a, b in zip(pivots, pivots[1:]):
        if a[2] == "H" and b[2] == "L":
            hi_p, lo_p = a[1], b[1]
            if hi_p <= 0:
                continue
            depth = (hi_p - lo_p) / hi_p * 100.0
            seg = volume[a[0]: b[0] + 1]
            contractions.append(Contraction(
                start_idx=a[0], end_idx=b[0],
                high=hi_p, low=lo_p,
                depth_pct=depth,
                days=max(1, b[0] - a[0]),
                avg_volume=float(np.nanmean(seg)) if seg.size else float("nan"),
            ))

    if len(contractions) < min_ct:
        res.reason = f"수축 {len(contractions)}회 (최소 {min_ct}회 필요)"
        return res

    # 베이스 시작점 탐색.
    #
    # 마지막 수축에서 끝나는 모든 꼬리 구간을 후보로 놓고 검증합니다.
    # 단순히 최근 N개를 자르면 베이스 이전 추세 구간의 잔 스윙이 섞여서
    # 단조감소 판정이 깨집니다. 실제 차트에서 항상 일어나는 일입니다.
    best_seq: list[Contraction] | None = None
    last_reason = "수축 조건 불충족"

    for start in range(len(contractions)):
        seq = contractions[start:]
        if not (min_ct <= len(seq) <= max_ct):
            continue

        ok, why = _validate(seq, min_base, max_base, max_first, max_last, tol)
        if not ok:
            last_reason = why
            continue

        # 유효한 후보 중에서는 수축 횟수가 많은 쪽을 택합니다.
        # start를 앞에서부터 도니 첫 유효 후보가 곧 최장 수열입니다.
        best_seq = seq
        break

    if best_seq is None:
        res.reason = last_reason
        return res

    contractions = best_seq
    res.contractions = contractions
    depths = [c.depth_pct for c in contractions]
    res.base_days = contractions[-1].end_idx - contractions[0].start_idx

    # 거래량도 함께 말라야 합니다
    v_first, v_last = contractions[0].avg_volume, contractions[-1].avg_volume
    if np.isfinite(v_first) and v_first > 0 and np.isfinite(v_last):
        res.volume_ratio = v_last / v_first
        if res.volume_ratio > max_vol_ratio:
            res.reason = f"거래량 미수축 ({res.volume_ratio:.2f})"
            return res

    # 피벗 직전 거래량 고갈.
    #
    # 반드시 '당일을 제외한' 구간으로 봐야 합니다. 돌파일에는 거래량이
    # 급증하는데, 그날을 고갈 판정에 넣으면 돌파가 영원히 탐지되지 않습니다.
    # 고갈은 돌파 직전의 상태를 묘사하는 조건이지 돌파 당일의 조건이 아닙니다.
    if len(volume) >= 52:
        v5 = float(np.nanmean(volume[-6:-1]))
        v50 = float(np.nanmean(volume[-51:-1]))
        if v50 > 0:
            res.dryup_ratio = v5 / v50
            if res.dryup_ratio > max_dryup:
                res.reason = f"거래량 고갈 부족 ({res.dryup_ratio:.2f})"
                return res

    # 피벗 = 베이스 내 최고 저항선.
    # 마지막 수축의 고점만 쓰면 앞선 수축의 더 높은 고점을 놓칩니다.
    res.pivot = float(max(c.high for c in contractions))
    last_close = float(close[-1])
    res.dist_to_pivot_pct = (last_close / res.pivot - 1.0) * 100.0

    if res.dist_to_pivot_pct < -max_dist:
        res.reason = f"피벗까지 {abs(res.dist_to_pivot_pct):.1f}% (상한 {max_dist:.0f}%)"
        return res

    # 이미 크게 뻗은 종목은 신규 셋업이 아닙니다. 미너비니가 반복해서
    # 경고하는 '연장된(extended)' 상태입니다.
    if res.dist_to_pivot_pct > max_extended:
        res.reason = f"피벗 대비 {res.dist_to_pivot_pct:.1f}% 연장됨"
        return res

    # 돌파 판정: 종가가 피벗을 넘고 거래량이 실렸는가.
    # 여기서의 v50은 당일을 제외한 평균이어야 비교가 공정합니다.
    if len(volume) >= 52:
        v50_prior = float(np.nanmean(volume[-51:-1]))
        res.breakout = bool(last_close > res.pivot and v50_prior > 0
                            and volume[-1] >= v50_prior * bo_mult)

    # 손절 라인 = 마지막 수축의 저점
    res.stop_price = float(contractions[-1].low)
    entry = max(last_close, res.pivot)
    if entry > 0:
        res.risk_pct = (1.0 - res.stop_price / entry) * 100.0

    res.detected = True
    res.score = _score(res, depths, max_last)
    res.reason = "VCP 성립"
    return res


def _validate(seq: list[Contraction], min_base: int, max_base: int,
              max_first: float, max_last: float, tol: float) -> tuple[bool, str]:
    """수축 수열 하나가 VCP 형태를 갖췄는지 검증합니다."""
    depths = [c.depth_pct for c in seq]
    base_days = seq[-1].end_idx - seq[0].start_idx

    if not (min_base <= base_days <= max_base):
        return False, f"베이스 {base_days}일 (허용 {min_base}~{max_base}일)"

    if not all(depths[i + 1] <= depths[i] * tol for i in range(len(depths) - 1)):
        return False, "수축폭이 줄지 않음"

    if depths[0] > max_first:
        return False, f"첫 조정 {depths[0]:.0f}% (상한 {max_first:.0f}%)"

    if depths[-1] > max_last:
        return False, f"마지막 조정 {depths[-1]:.0f}% (상한 {max_last:.0f}%)"

    return True, ""


def _score(res: VCPResult, depths: list[float], max_last: float) -> float:
    """0~100 점수. 워치리스트 정렬 우선순위로 씁니다."""
    score = 0.0

    # 수축 횟수 (3~4회가 이상적) — 20점
    n = len(depths)
    score += {2: 12.0, 3: 20.0, 4: 20.0}.get(n, 16.0 if n >= 5 else 8.0)

    # 마지막 수축이 얼마나 타이트한가 — 25점
    if max_last > 0:
        tightness = max(0.0, 1.0 - depths[-1] / max_last)
        score += 25.0 * tightness

    # 수축 감소가 얼마나 뚜렷한가 — 15점
    if depths[0] > 0:
        shrink = max(0.0, 1.0 - depths[-1] / depths[0])
        score += 15.0 * shrink

    # 거래량 수축 — 20점
    if np.isfinite(res.volume_ratio):
        score += 20.0 * max(0.0, min(1.0, 1.0 - res.volume_ratio))

    # 피벗 근접도 — 20점
    if np.isfinite(res.dist_to_pivot_pct):
        prox = max(0.0, 1.0 - abs(res.dist_to_pivot_pct) / 10.0)
        score += 20.0 * prox

    return round(min(100.0, score), 1)
