"""KRX 데이터 수집.

FinanceDataReader = 대량 과거 일봉 백필에 강함
pykrx            = 시가총액, 재무지표, 투자자별 수급, 지수

두 소스의 컬럼명이 버전마다 바뀌므로 정규화 계층을 한 겹 둡니다.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd

log = logging.getLogger(__name__)

# 한국 거래일 기준 대략치. 정확한 휴장일은 pykrx가 알아서 걸러줍니다.
TRADING_DAYS_PER_YEAR = 246


def _lazy_fdr():
    import FinanceDataReader as fdr
    return fdr


def _lazy_pykrx():
    from pykrx import stock
    return stock


def _retry(fn, tries: int = 3, sleep: float = 1.5, label: str = ""):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < tries - 1:
                time.sleep(sleep * (i + 1))
    log.warning("실패 %s: %s", label, last)
    return None


# --------------------------------------------------------------------
# 종목 리스트
# --------------------------------------------------------------------

def fetch_listing(markets: list[str]) -> pd.DataFrame:
    """
    전 종목 기본정보. code, name, market, marcap, shares 컬럼으로 정규화합니다.

    FDR이 KRX에 차단당하는 일이 있어(Akamai Access Denied) pykrx 경로를 예비로 둡니다.
    """
    fdr = _lazy_fdr()
    raw = _retry(lambda: fdr.StockListing("KRX"), label="StockListing")

    if raw is None or raw.empty:
        log.warning("FDR 종목 리스트 실패. pykrx로 다시 시도합니다.")
        alt = _fetch_listing_pykrx(markets)
        if alt is not None and not alt.empty:
            log.info("  pykrx로 %d종목 확보", len(alt))
            return alt
        raise RuntimeError(_listing_error_hint())

    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]

    # FDR 버전별 컬럼명 흡수
    rename_map = {
        "Code": "code", "Symbol": "code", "종목코드": "code",
        "Name": "name", "종목명": "name",
        "Market": "market", "시장구분": "market",
        "Marcap": "marcap", "MarketCap": "marcap", "시가총액": "marcap",
        "Stocks": "shares", "상장주식수": "shares", "ListedShares": "shares",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    for col in ("code", "name", "market"):
        if col not in df.columns:
            raise RuntimeError(f"종목 리스트에 '{col}' 컬럼이 없습니다. FDR 버전을 확인하세요.")

    if "marcap" not in df.columns:
        df["marcap"] = pd.NA
    if "shares" not in df.columns:
        df["shares"] = pd.NA

    df["code"] = df["code"].astype(str).str.zfill(6)
    df["market"] = df["market"].astype(str).str.upper()
    df = df[df["market"].isin([m.upper() for m in markets])]

    out = df[["code", "name", "market", "marcap", "shares"]].copy()
    out["marcap"] = pd.to_numeric(out["marcap"], errors="coerce").fillna(0).astype("int64")
    out["shares"] = pd.to_numeric(out["shares"], errors="coerce").fillna(0).astype("int64")
    out["updated_at"] = pd.Timestamp.today().date()
    return out.drop_duplicates("code").reset_index(drop=True)


def _fetch_listing_pykrx(markets: list[str]) -> pd.DataFrame | None:
    """pykrx로 종목 리스트를 만듭니다. 시가총액 테이블에 티커와 상장주식수가 함께 옵니다."""
    stock = _lazy_pykrx()
    as_of = recent_business_day()
    frames = []

    for market in markets:
        cap = _retry(lambda m=market: stock.get_market_cap(as_of, market=m),
                     label=f"market_cap {market}")
        if cap is None or cap.empty:
            continue
        d = cap.reset_index()
        d = d.rename(columns={d.columns[0]: "code", "시가총액": "marcap",
                              "상장주식수": "shares"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["market"] = market.upper()
        for col in ("marcap", "shares"):
            if col not in d.columns:
                d[col] = 0
            d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0).astype("int64")
        frames.append(d[["code", "market", "marcap", "shares"]])

    if not frames:
        return None

    out = pd.concat(frames, ignore_index=True).drop_duplicates("code")

    # 종목명은 티커별 호출이라 병렬로 받습니다.
    names: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(stock.get_market_ticker_name, c): c
            for c in out["code"].tolist()
        }
        for fut in as_completed(futures):
            code = futures[fut]
            try:
                names[code] = str(fut.result())
            except Exception:  # noqa: BLE001
                names[code] = code

    out["name"] = out["code"].map(names).fillna(out["code"])
    out["updated_at"] = pd.Timestamp.today().date()
    return out[["code", "name", "market", "marcap", "shares", "updated_at"]].reset_index(drop=True)


def _listing_error_hint() -> str:
    """실패 원인을 짚어주는 메시지를 만듭니다. 그냥 '네트워크 확인'은 도움이 안 됩니다."""
    lines = ["종목 리스트를 가져오지 못했습니다."]

    try:
        import certifi
        bundle = certifi.where()
        import os
        if not os.path.exists(bundle):
            lines.append("  certifi 인증서 번들이 없습니다. pip install --upgrade certifi 를 실행하세요.")
        else:
            lines.append(f"  인증서 번들: {bundle}")
    except ImportError:
        lines.append("  certifi가 설치되어 있지 않습니다. pip install certifi 를 실행하세요.")

    lines += [
        "",
        "  자주 있는 원인",
        "   1. 맥 인증서 미등록 — 응용 프로그램 > Python 3.x 폴더의",
        "      'Install Certificates.command'를 한 번 실행하세요.",
        "   2. 사내 방화벽 또는 프록시 — 회사망이면 개인 네트워크에서 시도해보세요.",
        "   3. KRX 서버 점검 — 잠시 후 다시 시도하세요.",
    ]
    return "\n".join(lines)


def fetch_admin_issues(as_of: str) -> set[str]:
    """관리종목 코드 집합. pykrx가 제공하지 않는 버전이면 빈 집합을 돌려줍니다."""
    stock = _lazy_pykrx()
    codes: set[str] = set()
    for market in ("KOSPI", "KOSDAQ"):
        for getter in ("get_market_ticker_list",):
            fn = getattr(stock, getter, None)
            if fn is None:
                continue
            try:
                admin = fn(as_of, market=market, alternative=True)  # type: ignore[call-arg]
                if admin:
                    codes |= {str(c).zfill(6) for c in admin}
            except TypeError:
                pass
            except Exception:  # noqa: BLE001
                pass
    return codes


# --------------------------------------------------------------------
# 일봉
# --------------------------------------------------------------------

def _normalize_ohlcv(df: pd.DataFrame, code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    rename = {
        "Open": "open", "시가": "open",
        "High": "high", "고가": "high",
        "Low": "low", "저가": "low",
        "Close": "close", "종가": "close",
        "Volume": "volume", "거래량": "volume",
        "Amount": "value", "거래대금": "value",
    }
    d = d.rename(columns={k: v for k, v in rename.items() if k in d.columns})
    for col in ("open", "high", "low", "close", "volume", "value"):
        if col not in d.columns:
            d[col] = pd.NA
    d = d.reset_index().rename(columns={d.index.name or "index": "date",
                                        "Date": "date", "날짜": "date"})
    if "date" not in d.columns:
        d["date"] = pd.to_datetime(df.index)
    d["date"] = pd.to_datetime(d["date"]).dt.date
    d["code"] = code
    d = d[["code", "date", "open", "high", "low", "close", "volume", "value"]]
    for col in ("open", "high", "low", "close"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ("volume", "value"):
        d[col] = pd.to_numeric(d[col], errors="coerce").fillna(0).astype("int64")

    # FinanceDataReader는 거래대금(Amount)을 주지 않습니다.
    # 없으면 종가 x 거래량으로 대체합니다. 유동성 필터에 쓰기에 충분한 근사치입니다.
    # 이걸 빼먹으면 value가 전부 0이 되어 거래대금 필터가 전 종목을 걸러냅니다.
    missing = d["value"] <= 0
    if missing.any():
        est = (d.loc[missing, "close"].fillna(0) * d.loc[missing, "volume"]).fillna(0)
        d.loc[missing, "value"] = est.astype("int64")

    return d.dropna(subset=["close"])


def fetch_ohlcv_one(code: str, start: str, end: str,
                    tries: int = 3, sleep: float = 1.5) -> pd.DataFrame:
    fdr = _lazy_fdr()
    raw = _retry(lambda: fdr.DataReader(code, start, end),
                 tries=tries, sleep=sleep, label=f"OHLCV {code}")
    return _normalize_ohlcv(raw, code)


def fetch_ohlcv_bulk(codes: list[str], start: str, end: str,
                     max_workers: int = 8, tries: int = 3,
                     sleep: float = 1.5, progress: bool = True,
                     on_chunk=None, chunk_size: int = 150) -> pd.DataFrame:
    """
    여러 종목 일봉을 병렬로 받습니다. 개별 실패는 건너뛰고 계속 진행합니다.

    on_chunk를 주면 chunk_size 종목마다 그때까지 모은 데이터를 넘겨줍니다.
    호출부에서 즉시 DB에 쓰면 중간에 끊겨도 처음부터 다시 받지 않아도 됩니다.
    """
    frames: list[pd.DataFrame] = []
    pending: list[pd.DataFrame] = []
    done = 0
    total = len(codes)
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_ohlcv_one, c, start, end, tries, sleep): c
            for c in codes
        }
        for fut in as_completed(futures):
            done += 1
            try:
                df = fut.result()
                if not df.empty:
                    pending.append(df)
                    if on_chunk is None:
                        frames.append(df)
            except Exception as exc:  # noqa: BLE001
                log.warning("건너뜀 %s: %s", futures[fut], exc)

            if on_chunk is not None and len(pending) >= chunk_size:
                on_chunk(pd.concat(pending, ignore_index=True))
                pending = []

            if progress and (done % 50 == 0 or done == total):
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                remain = (total - done) / rate if rate > 0 else 0
                log.info("  %4d/%d  (%2.0f%%)  %.1f종목/초  남은시간 약 %d분",
                         done, total, done / total * 100, rate, int(remain / 60) + 1)

    if on_chunk is not None:
        if pending:
            on_chunk(pd.concat(pending, ignore_index=True))
        return pd.DataFrame()

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def fetch_daily_snapshot(as_of: str, markets: list[str]) -> pd.DataFrame:
    """하루치 전 종목 일봉. 증분 갱신용으로 API 호출 1~2번이면 끝납니다."""
    stock = _lazy_pykrx()
    frames = []
    for market in markets:
        df = _retry(lambda m=market: stock.get_market_ohlcv(as_of, market=m),
                    label=f"snapshot {market}")
        if df is None or df.empty:
            continue
        d = df.reset_index().rename(columns={"티커": "code"})
        d = d.rename(columns={
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume", "거래대금": "value",
        })
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["date"] = pd.to_datetime(as_of).date()
        keep = ["code", "date", "open", "high", "low", "close", "volume", "value"]
        d = d[[c for c in keep if c in d.columns]]
        frames.append(d)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    # 거래정지 종목은 거래량 0으로 들어옵니다. 가격이 0이면 버립니다.
    return out[out["close"] > 0].reset_index(drop=True)


# --------------------------------------------------------------------
# 재무지표 / 수급 / 지수
# --------------------------------------------------------------------

def fetch_fundamentals(as_of: str, markets: list[str]) -> pd.DataFrame:
    stock = _lazy_pykrx()
    frames = []
    for market in markets:
        df = _retry(lambda m=market: stock.get_market_fundamental(as_of, market=m),
                    label=f"fundamental {market}")
        if df is None or df.empty:
            continue
        d = df.reset_index().rename(columns={"티커": "code"})
        d = d.rename(columns={"BPS": "bps", "PER": "per", "PBR": "pbr",
                              "EPS": "eps", "DIV": "div"})
        d["code"] = d["code"].astype(str).str.zfill(6)
        d["date"] = pd.to_datetime(as_of).date()
        cols = ["code", "date", "bps", "per", "pbr", "eps", "div"]
        for c in cols:
            if c not in d.columns:
                d[c] = pd.NA
        frames.append(d[cols])
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    for c in ("bps", "per", "pbr", "eps", "div"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def fetch_flows_one(code: str, start: str, end: str) -> pd.DataFrame:
    """종목별 투자자 순매수. 컬럼명이 버전마다 달라 방어적으로 매핑합니다."""
    stock = _lazy_pykrx()
    df = _retry(
        lambda: stock.get_market_trading_value_by_date(start, end, code),
        label=f"flow {code}",
    )
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.reset_index()
    date_col = d.columns[0]
    d = d.rename(columns={date_col: "date"})
    d["date"] = pd.to_datetime(d["date"]).dt.date

    def pick(*names):
        for n in names:
            if n in d.columns:
                return pd.to_numeric(d[n], errors="coerce").fillna(0)
        return pd.Series(0, index=d.index)

    out = pd.DataFrame({
        "code": code,
        "date": d["date"],
        "inst_net": pick("기관합계", "기관"),
        "frgn_net": pick("외국인합계", "외국인"),
        "retail_net": pick("개인"),
    })
    for c in ("inst_net", "frgn_net", "retail_net"):
        out[c] = out[c].astype("int64")
    return out


def fetch_flows_bulk(codes: list[str], start: str, end: str,
                     max_workers: int = 6, progress: bool = True) -> pd.DataFrame:
    frames = []
    done = 0
    total = len(codes)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_flows_one, c, start, end): c for c in codes}
        for fut in as_completed(futures):
            done += 1
            try:
                df = fut.result()
                if not df.empty:
                    frames.append(df)
            except Exception:  # noqa: BLE001
                pass
            if progress and (done % 100 == 0 or done == total):
                log.info("  수급 %d/%d (%2.0f%%)", done, total, done / total * 100)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_index(code: str, start: str, end: str) -> pd.DataFrame:
    stock = _lazy_pykrx()
    df = _retry(lambda: stock.get_index_ohlcv(start, end, code), label=f"index {code}")
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.reset_index()
    d = d.rename(columns={d.columns[0]: "date", "종가": "close", "거래량": "volume"})
    d["date"] = pd.to_datetime(d["date"]).dt.date
    d["code"] = code
    if "volume" not in d.columns:
        d["volume"] = 0
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d["volume"] = pd.to_numeric(d["volume"], errors="coerce").fillna(0).astype("int64")
    return d[["code", "date", "close", "volume"]].dropna(subset=["close"])


def prefilter_codes(listing: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    다운로드 전에 걸러냅니다.

    이게 없으면 어차피 유동성·시총 필터에서 탈락할 소형주까지 전부 받습니다.
    2,700종목이 1,200종목대로 줄어들어 백필 시간이 절반 이하가 됩니다.
    가격 데이터가 필요한 필터(거래대금 등)는 여기서 못 쓰므로,
    종목명·코드·시가총액만으로 판단합니다.
    """
    from .universe import is_preferred

    df = listing.copy()
    before = len(df)

    if cfg.get("universe.exclude_preferred", True):
        df = df[~df.apply(lambda r: is_preferred(str(r["code"]), str(r["name"])), axis=1)]

    blacklist = cfg.get("universe.name_blacklist", []) or []
    if blacklist:
        df = df[~df["name"].astype(str).str.contains("|".join(blacklist), na=False)]

    if cfg.get("universe.exclude_spac", True):
        df = df[~df["name"].astype(str).str.contains("스팩", na=False)]

    if cfg.get("universe.exclude_etf_etn_reit", True):
        df = df[~df["name"].astype(str).str.contains(
            r"ETF|ETN|리츠|인버스|레버리지|KODEX|TIGER|KBSTAR|ARIRANG|HANARO",
            na=False, case=False, regex=True)]

    # 시총 하한을 여유 있게 적용합니다. 경계선 종목이 나중에 오르는 걸 감안해
    # 실제 필터의 70% 수준에서 자릅니다.
    min_cap = cfg.get("universe.min_marcap_eok", 1000) * 0.7 * 100_000_000
    if "marcap" in df.columns and pd.to_numeric(df["marcap"], errors="coerce").fillna(0).max() > 0:
        df = df[pd.to_numeric(df["marcap"], errors="coerce").fillna(0) >= min_cap]

    log.info("다운로드 대상 %d → %d종목 (소형주·우선주·ETF 사전 제외)", before, len(df))
    return df.reset_index(drop=True)


def recent_business_day(ref: date | None = None) -> str:
    """주말이면 직전 금요일로 되돌립니다. 공휴일은 pykrx 응답이 비면 알아서 걸립니다."""
    d = ref or datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")
