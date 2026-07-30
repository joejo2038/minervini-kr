"""재무 필터.

미너비니의 펀더멘털 축은 실적 가속입니다. EPS 증가율, 매출 증가율,
마진 확대. 문제는 pykrx가 스냅샷 PER/EPS만 주고 분기 추이를 안 준다는 점입니다.

두 단계로 나눴습니다.
  기본  : pykrx 스냅샷으로 적자기업 제외 + PER 상한 (API 키 불필요)
  심화  : DART OpenAPI로 분기 EPS 전년동기비 증가율 (무료 키 필요)

DART 키가 없으면 심화 단계는 조용히 건너뛰고 기본만 적용합니다.
키 발급: https://opendart.fss.or.kr
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

log = logging.getLogger(__name__)

DART_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"


def basic_filter(fundamentals: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    Returns
    -------
    code 인덱스 DataFrame: per, pbr, eps, profitable, fund_pass
    """
    if fundamentals is None or fundamentals.empty:
        return pd.DataFrame(columns=["per", "pbr", "eps", "profitable", "fund_pass"])

    df = fundamentals.copy().set_index("code")
    for col in ("per", "pbr", "eps"):
        if col not in df.columns:
            df[col] = pd.NA
        df[col] = pd.to_numeric(df[col], errors="coerce")

    out = pd.DataFrame(index=df.index)
    out["per"] = df["per"].round(2)
    out["pbr"] = df["pbr"].round(2)
    out["eps"] = df["eps"]
    out["profitable"] = (df["eps"].fillna(0) > 0)

    ok = pd.Series(True, index=df.index)
    if cfg.get("fundamentals.require_profitable", True):
        ok &= out["profitable"]

    max_per = cfg.get("fundamentals.max_per", 0) or 0
    if max_per > 0:
        ok &= df["per"].fillna(1e9).between(0, max_per, inclusive="both")

    out["fund_pass"] = ok
    return out


# --------------------------------------------------------------------
# DART 분기 실적 (선택)
# --------------------------------------------------------------------

def _dart_corp_map(api_key: str) -> dict[str, str]:
    """종목코드 → DART 고유번호 매핑. corpCode.xml zip을 받아 파싱합니다."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    try:
        r = requests.get(url, params={"crtfc_key": api_key}, timeout=30)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml = z.read(z.namelist()[0])
    except Exception as exc:  # noqa: BLE001
        log.warning("DART 기업코드 조회 실패: %s", exc)
        return {}

    mapping: dict[str, str] = {}
    for item in ET.fromstring(xml).iter("list"):
        stock_code = (item.findtext("stock_code") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if stock_code and corp_code:
            mapping[stock_code.zfill(6)] = corp_code
    return mapping


def _fetch_eps_growth(corp_code: str, api_key: str, year: int) -> float | None:
    """최근 분기 당기순이익의 전년동기 대비 증가율. 실패하면 None."""
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": "11014",  # 3분기 보고서
        "fs_div": "CFS",
    }
    try:
        r = requests.get(DART_URL, params=params, timeout=15)
        data = r.json()
        if data.get("status") != "000":
            return None
        for row in data.get("list", []):
            if "당기순이익" not in row.get("account_nm", ""):
                continue
            cur = str(row.get("thstrm_amount", "")).replace(",", "")
            prev = str(row.get("frmtrm_amount", "")).replace(",", "")
            if not cur or not prev:
                return None
            cur_f, prev_f = float(cur), float(prev)
            if prev_f <= 0:
                return None
            return cur_f / prev_f - 1.0
    except Exception:  # noqa: BLE001
        return None
    return None


def dart_growth(codes: list[str], cfg, max_workers: int = 4) -> pd.DataFrame:
    """DART 분기 성장률. 키가 없거나 비활성이면 빈 프레임을 반환합니다."""
    empty = pd.DataFrame(columns=["eps_growth_yoy", "growth_pass"])

    if not cfg.get("fundamentals.dart_enabled", False):
        return empty

    key_env = cfg.get("fundamentals.dart_api_key_env", "DART_API_KEY")
    api_key = os.environ.get(key_env, "")
    if not api_key:
        log.info("DART 키(%s)가 없어 심화 재무 필터를 건너뜁니다.", key_env)
        return empty

    mapping = _dart_corp_map(api_key)
    if not mapping:
        return empty

    year = pd.Timestamp.today().year - 1
    rows: list[tuple[str, float]] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for c in codes:
            corp = mapping.get(c)
            if corp:
                futures[pool.submit(_fetch_eps_growth, corp, api_key, year)] = c
        for fut in as_completed(futures):
            g = fut.result()
            if g is not None:
                rows.append((futures[fut], g))
            time.sleep(0.02)  # DART 분당 호출 제한 배려

    if not rows:
        return empty

    out = pd.DataFrame(rows, columns=["code", "eps_growth_yoy"]).set_index("code")
    threshold = cfg.get("fundamentals.min_eps_growth_yoy", 0.20)
    out["growth_pass"] = out["eps_growth_yoy"] >= threshold
    out["eps_growth_yoy"] = (out["eps_growth_yoy"] * 100).round(1)
    return out
