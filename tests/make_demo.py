"""합성 데이터로 파이프라인 전체를 검증합니다.

KRX에 접속하지 않고도 지표 계산 → Trend Template → VCP → JSON 출력까지
전 과정이 도는지 확인할 수 있습니다. 배포 전 스모크 테스트로 쓰세요.

    python run_screen.py --demo
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src import report as report_mod
from src import screener
from src.db import Store

log = logging.getLogger(__name__)
rng = np.random.default_rng(20260722)

EOK = 100_000_000


def _ohlc_from_close(close: np.ndarray, noise: float = 0.012) -> dict:
    n = len(close)
    wig = np.abs(rng.normal(0, noise, n)) * close
    high = close + wig
    low = close - np.abs(rng.normal(0, noise, n)) * close
    open_ = close + rng.normal(0, noise * 0.6, n) * close
    low = np.minimum.reduce([low, open_, close])
    high = np.maximum.reduce([high, open_, close])
    return {"open": open_, "high": high, "low": low, "close": close}


def _make_vcp(n: int = 620) -> tuple[np.ndarray, np.ndarray]:
    """상승 추세 후 3단 수축 베이스를 만든 시계열."""
    base_len = 60
    trend_len = n - base_len

    # 1. 장기 상승
    drift = rng.uniform(0.0011, 0.0018)
    steps = rng.normal(drift, 0.016, trend_len)
    trend = 10000 * np.exp(np.cumsum(steps))
    peak = float(trend[-1])

    # 2. 수축 베이스: 깊이가 계단식으로 줄어듭니다
    depths = [rng.uniform(0.20, 0.26), rng.uniform(0.11, 0.15), rng.uniform(0.05, 0.07)]
    seg_lens = [24, 20, 16]
    base_parts = []
    level = peak
    for depth, seg in zip(depths, seg_lens):
        down = int(seg * 0.6)
        up = seg - down
        low_pt = level * (1 - depth)
        leg_down = np.linspace(level, low_pt, down) * (1 + rng.normal(0, 0.006, down))
        leg_up = np.linspace(low_pt, level * 0.995, up) * (1 + rng.normal(0, 0.005, up))
        base_parts += [leg_down, leg_up]
        level = level * 0.997

    base = np.concatenate(base_parts)[:base_len]
    if len(base) < base_len:
        base = np.pad(base, (0, base_len - len(base)), mode="edge")

    close = np.concatenate([trend, base])

    # 3. 거래량: 베이스 구간에서 계단식 감소
    vol_trend = rng.lognormal(13.0, 0.35, trend_len)
    vol_base = np.concatenate([
        rng.lognormal(13.0, 0.3, seg_lens[0]) * 0.95,
        rng.lognormal(13.0, 0.3, seg_lens[1]) * 0.62,
        rng.lognormal(13.0, 0.3, seg_lens[2]) * 0.38,
    ])[:base_len]
    if len(vol_base) < base_len:
        vol_base = np.pad(vol_base, (0, base_len - len(vol_base)), mode="edge")

    return close, np.concatenate([vol_trend, vol_base])


def _make_trend(n: int = 620) -> tuple[np.ndarray, np.ndarray]:
    """VCP 없는 단순 상승 추세. 1단계는 통과하고 2단계에서 걸립니다."""
    steps = rng.normal(rng.uniform(0.0009, 0.0016), 0.019, n)
    close = 10000 * np.exp(np.cumsum(steps))
    return close, rng.lognormal(13.0, 0.4, n)


def _make_downtrend(n: int = 620) -> tuple[np.ndarray, np.ndarray]:
    """하락 추세. 1단계에서 걸러져야 합니다."""
    steps = rng.normal(-0.0011, 0.021, n)
    close = 30000 * np.exp(np.cumsum(steps))
    return close, rng.lognormal(12.8, 0.45, n)


def build_demo(cfg) -> None:
    n_days = 620
    dates = pd.bdate_range(end=pd.Timestamp("2026-07-22"), periods=n_days).date

    specs = (
        [("vcp", i) for i in range(18)]
        + [("trend", i) for i in range(60)]
        + [("down", i) for i in range(92)]
    )

    listing_rows, price_frames, fund_rows, flow_frames = [], [], [], []

    for idx, (kind, seq) in enumerate(specs):
        # 끝자리를 0으로 고정합니다. 5/7/9로 끝나면 우선주 필터에 걸립니다.
        code = f"{900000 + idx * 10:06d}"
        market = "KOSPI" if idx % 3 == 0 else "KOSDAQ"
        label = {"vcp": "브이씨피", "trend": "추세", "down": "하락"}[kind]
        name = f"{label}테크{seq:02d}"

        if kind == "vcp":
            close, volume = _make_vcp(n_days)
        elif kind == "trend":
            close, volume = _make_trend(n_days)
        else:
            close, volume = _make_downtrend(n_days)

        ohlc = _ohlc_from_close(close)
        value = (close * volume).astype("int64")

        price_frames.append(pd.DataFrame({
            "code": code,
            "date": dates,
            "open": ohlc["open"],
            "high": ohlc["high"],
            "low": ohlc["low"],
            "close": ohlc["close"],
            "volume": volume.astype("int64"),
            "value": value,
        }))

        shares = int(rng.integers(8_000_000, 90_000_000))
        listing_rows.append({
            "code": code, "name": name, "market": market,
            "marcap": int(close[-1] * shares), "shares": shares,
            "updated_at": dates[-1],
        })

        eps = float(rng.uniform(300, 4200)) if kind != "down" else float(rng.uniform(-800, 400))
        fund_rows.append({
            "code": code, "date": dates[-1],
            "bps": float(rng.uniform(5000, 60000)),
            "per": round(float(close[-1] / eps), 2) if eps > 0 else -1.0,
            "pbr": round(float(rng.uniform(0.5, 6.0)), 2),
            "eps": eps,
            "div": round(float(rng.uniform(0, 3)), 2),
        })

        sign = 1 if kind in ("vcp", "trend") else -1
        flow_dates = dates[-25:]
        flow_frames.append(pd.DataFrame({
            "code": code,
            "date": flow_dates,
            "inst_net": (rng.normal(sign * 4e8, 6e8, len(flow_dates))).astype("int64"),
            "frgn_net": (rng.normal(sign * 3e8, 5e8, len(flow_dates))).astype("int64"),
            "retail_net": (rng.normal(-sign * 5e8, 7e8, len(flow_dates))).astype("int64"),
        }))

    # 지수: 완만한 상승
    idx_frames = []
    for code, base in (("1001", 2600.0), ("2001", 780.0)):
        steps = rng.normal(0.0006, 0.008, n_days)
        idx_frames.append(pd.DataFrame({
            "code": code, "date": dates,
            "close": base * np.exp(np.cumsum(steps)),
            "volume": rng.integers(3e8, 9e8, n_days).astype("int64"),
        }))

    db_path = cfg.path("data.db_path", "data/market.duckdb").parent / "demo.duckdb"
    if db_path.exists():
        db_path.unlink()

    with Store(db_path) as store:
        store.upsert("listing", pd.DataFrame(listing_rows), ["code"])
        store.upsert("prices", pd.concat(price_frames, ignore_index=True), ["code", "date"])
        store.upsert("fundamentals", pd.DataFrame(fund_rows), ["code", "date"])
        store.upsert("flows", pd.concat(flow_frames, ignore_index=True), ["code", "date"])
        store.upsert("indices", pd.concat(idx_frames, ignore_index=True), ["code", "date"])

        log.info("합성 데이터 적재 완료: %d종목 %d행",
                 len(listing_rows), store.row_count("prices"))

        result = screener.run(store, cfg)
        result["demo"] = True  # 프론트에 경고 배너를 띄우는 플래그
        report_mod.write(result, cfg)
        _seed_demo_internals(cfg, result)


def _seed_demo_internals(cfg, result) -> None:
    """데모에서 우측 추이 그래프가 보이도록 가짜 시계열을 심습니다."""
    import json
    import random
    from datetime import date, timedelta

    random.seed(1)
    path = cfg.path("output.json_path").parent / "internals.json"
    base = date(2026, 7, 8)
    series = []
    b = result["regime"]["breadth_above_ma200"] - 12
    s1 = max(5, result["stats"]["stage1"] - 14)
    for i in range(12):
        b = max(15, b + random.uniform(-1.5, 3.0))
        s1 = max(3, s1 + random.randint(-2, 3))
        series.append({
            "date": str(base + timedelta(days=i)),
            "breadth": round(b, 1), "stage1": s1,
            "stage2": random.randint(0, 4), "breakout": random.randint(0, 1),
            "verdict": "caution",
        })
    # 마지막 포인트는 실제 결과로 맞춰줍니다
    series[-1]["breadth"] = result["regime"]["breadth_above_ma200"]
    series[-1]["stage1"] = result["stats"]["stage1"]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"series": series}, f, ensure_ascii=False)
        print()
        print(report_mod.summary_text(result))
