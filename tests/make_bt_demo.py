"""
백테스트 검증용 장기 합성 데이터.

make_demo는 하루치 스크리닝 검증용이라 기간이 짧습니다. 백테스트는
워밍업 252일 + 테스트 구간이 필요하므로 4년치를 만듭니다.

시장 사이클(강세 → 조정 → 강세)을 넣어 국면별 성과 분리가 실제로
동작하는지 확인합니다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.db import Store

log = logging.getLogger(__name__)
rng = np.random.default_rng(7)


def _market_cycle(n: int) -> np.ndarray:
    """강세-조정-강세-조정 사이클의 일간 드리프트."""
    seg = n // 4
    parts = [
        np.full(seg, 0.0012),      # 강세
        np.full(seg, -0.0015),     # 조정
        np.full(seg, 0.0014),      # 강세
        np.full(n - 3 * seg, -0.0008),  # 약세
    ]
    return np.concatenate(parts)


def _stock(n: int, beta: float, alpha: float, market: np.ndarray,
           vcp_every: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """시장에 연동되면서 종목 고유 움직임을 가진 시계열."""
    idio = rng.normal(alpha, 0.020, n)
    steps = beta * market + idio
    close = 15000 * np.exp(np.cumsum(steps))
    volume = rng.lognormal(12.8, 0.42, n)

    # 주기적으로 VCP 형태의 수축 베이스를 심습니다.
    if vcp_every:
        i = 300
        while i + 70 < n:
            peak = close[i]
            depths = [0.22, 0.13, 0.07]
            lens = [22, 16, 12]
            pos = i
            for d, L in zip(depths, lens):
                down, up = int(L * 0.6), L - int(L * 0.6)
                lowpt = peak * (1 - d)
                close[pos:pos + down] = np.linspace(peak, lowpt, down) * (
                    1 + rng.normal(0, 0.005, down))
                close[pos + down:pos + L] = np.linspace(lowpt, peak * 0.995, up) * (
                    1 + rng.normal(0, 0.004, up))
                volume[pos:pos + L] *= (0.95 if d > 0.2 else 0.6 if d > 0.1 else 0.35)
                pos += L
            # 돌파 후 상승
            run = min(45, n - pos)
            if run > 0:
                close[pos:pos + run] = peak * np.exp(
                    np.cumsum(rng.normal(0.004, 0.017, run)))
                volume[pos] *= 2.4
            # 돌파 이후 구간은 이어지는 값에 반영
            if pos + run < n:
                shift = close[pos + run - 1] / close[pos + run] if close[pos + run] else 1
                close[pos + run:] *= shift
            i += vcp_every
    return close, volume


def build(cfg) -> None:
    n = 1000  # 약 4년
    dates = pd.bdate_range(end=pd.Timestamp("2026-07-24"), periods=n).date
    market = _market_cycle(n)

    listing, prices, funds = [], [], []

    specs = (
        [("리더", 40, 1.1, 0.0008, 160)] +
        [("추종", 60, 1.0, 0.0002, 0)] +
        [("부진", 50, 0.9, -0.0006, 0)]
    )

    idx = 0
    for label, count, beta, alpha, vcp_every in specs:
        for k in range(count):
            code = f"{800000 + idx * 10:06d}"
            idx += 1
            close, volume = _stock(n, beta, alpha, market, vcp_every)
            close = np.maximum(close, 500)

            wig = np.abs(rng.normal(0, 0.011, n)) * close
            high = close + wig
            low = close - np.abs(rng.normal(0, 0.011, n)) * close
            openp = close * (1 + rng.normal(0, 0.006, n))
            low = np.minimum.reduce([low, openp, close])
            high = np.maximum.reduce([high, openp, close])
            vol = volume.astype("int64")

            prices.append(pd.DataFrame({
                "code": code, "date": dates,
                "open": openp, "high": high, "low": low, "close": close,
                "volume": vol, "value": (close * vol).astype("int64"),
            }))

            shares = int(rng.integers(15_000_000, 90_000_000))
            listing.append({
                "code": code, "name": f"{label}{k:02d}",
                "market": "KOSPI" if idx % 2 else "KOSDAQ",
                "marcap": int(close[-1] * shares), "shares": shares,
                "updated_at": dates[-1],
            })
            eps = float(rng.uniform(500, 4000))
            funds.append({
                "code": code, "date": dates[-1],
                "bps": float(rng.uniform(8000, 50000)),
                "per": round(float(close[-1] / eps), 2),
                "pbr": round(float(rng.uniform(0.6, 5)), 2),
                "eps": eps, "div": 1.0,
            })

    idx_frames = []
    for code, base in (("1001", 2500.0), ("2001", 750.0)):
        idx_frames.append(pd.DataFrame({
            "code": code, "date": dates,
            "close": base * np.exp(np.cumsum(market + rng.normal(0, 0.004, n))),
            "volume": rng.integers(3e8, 9e8, n).astype("int64"),
        }))

    db_path = cfg.path("data.db_path").parent / "bt_demo.duckdb"
    if db_path.exists():
        db_path.unlink()

    with Store(db_path) as store:
        store.upsert("listing", pd.DataFrame(listing), ["code"])
        store.upsert("prices", pd.concat(prices, ignore_index=True), ["code", "date"])
        store.upsert("fundamentals", pd.DataFrame(funds), ["code", "date"])
        store.upsert("indices", pd.concat(idx_frames, ignore_index=True), ["code", "date"])
        log.info("합성 데이터: %d종목 %d행 (%s ~ %s)",
                 len(listing), store.row_count("prices"), dates[0], dates[-1])
    return db_path
