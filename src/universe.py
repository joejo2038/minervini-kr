"""유니버스 정제.

여기를 대충 하면 결과 전체가 쓰레기가 됩니다. 우선주와 스팩이 상위권에
올라오는 스크리너를 여러 번 봤습니다. 한국 시장은 노이즈 종목 비중이
높아서 이 단계가 사실상 절반입니다.
"""
from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger(__name__)

EOK = 100_000_000  # 1억


def is_preferred(code: str, name: str) -> bool:
    """
    우선주 판별.
      - 보통주는 종목코드가 0으로 끝납니다.
      - 우선주는 5(구형), 7/9(신형), K/L/M(3우선주 이상)으로 끝납니다.
      - 종목명에 '우'가 붙는 것도 함께 봅니다. ('대우', '한우' 같은 오탐 방지)
    """
    if not code:
        return False
    tail = code[-1].upper()
    if tail in {"5", "7", "9", "K", "L", "M"}:
        return True
    if name:
        n = name.strip()
        if n.endswith("우") and len(n) > 1:
            return True
        if n.endswith(("우B", "우C", "(전환)", "우선주")):
            return True
    return False


def apply(listing: pd.DataFrame, prices: pd.DataFrame, cfg,
          admin_codes: set[str] | None = None) -> pd.DataFrame:
    """
    Returns
    -------
    정제된 listing DataFrame. 추가 컬럼:
        avg_value_20d, listed_days, last_close
    """
    df = listing.copy()
    start_n = len(df)
    dropped: dict[str, int] = {}

    def drop(mask: pd.Series, label: str) -> None:
        nonlocal df
        n = int(mask.sum())
        if n:
            dropped[label] = n
            df = df[~mask].copy()

    # --- 이름 / 코드 기반 ---------------------------------------
    if cfg.get("universe.exclude_preferred", True):
        mask = df.apply(lambda r: is_preferred(str(r["code"]), str(r["name"])), axis=1)
        drop(mask, "우선주")

    blacklist = cfg.get("universe.name_blacklist", []) or []
    if blacklist:
        pattern = "|".join(blacklist)
        drop(df["name"].astype(str).str.contains(pattern, na=False, regex=True), "이름 블랙리스트")

    if cfg.get("universe.exclude_spac", True):
        drop(df["name"].astype(str).str.contains("스팩", na=False), "스팩")

    if cfg.get("universe.exclude_etf_etn_reit", True):
        drop(df["name"].astype(str).str.contains(
            r"ETF|ETN|리츠|인버스|레버리지|KODEX|TIGER|KBSTAR|ARIRANG|HANARO|SOL |ACE ",
            na=False, case=False, regex=True), "ETF/ETN/리츠")

    if admin_codes and cfg.get("universe.exclude_admin_issue", True):
        drop(df["code"].isin(admin_codes), "관리종목")

    # --- 가격 데이터 기반 ---------------------------------------
    if prices is None or prices.empty:
        log.warning("가격 데이터가 없어 유동성 필터를 건너뜁니다.")
        return df.reset_index(drop=True)

    p = prices.copy()
    p["date"] = pd.to_datetime(p["date"])

    agg = p.groupby("code").agg(
        listed_days=("date", "count"),
        last_date=("date", "max"),
    ).reset_index()

    recent_cut = p["date"].max() - pd.Timedelta(days=40)
    recent = p[p["date"] > recent_cut]
    tail20 = recent.sort_values("date").groupby("code").tail(20)
    liq = tail20.groupby("code").agg(
        avg_value_20d=("value", "mean"),
        last_close=("close", "last"),
    ).reset_index()

    df = df.merge(agg, on="code", how="left").merge(liq, on="code", how="left")

    drop(df["listed_days"].isna(), "가격 데이터 없음")
    drop(df["listed_days"] < cfg.get("universe.min_listed_days", 260), "상장 기간 부족")

    # 최근 거래가 끊긴 종목 = 거래정지
    last_market_date = p["date"].max()
    stale = df["last_date"] < (last_market_date - pd.Timedelta(days=10))
    drop(stale.fillna(True), "거래정지 의심")

    min_price = cfg.get("universe.min_price", 1000)
    drop(df["last_close"].fillna(0) < min_price, f"주가 {min_price}원 미만")

    min_value = cfg.get("universe.min_avg_value_eok", 10) * EOK
    drop(df["avg_value_20d"].fillna(0) < min_value, "거래대금 부족")

    min_cap = cfg.get("universe.min_marcap_eok", 1000) * EOK
    if "marcap" in df.columns and df["marcap"].fillna(0).max() > 0:
        drop(df["marcap"].fillna(0) < min_cap, "시가총액 부족")

    log.info("유니버스 %d → %d", start_n, len(df))
    for label, n in dropped.items():
        log.info("   -%-16s %d", label, n)

    return df.reset_index(drop=True)
