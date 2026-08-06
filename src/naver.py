"""
네이버 금융 일봉 어댑터.

KRX(FinanceDataReader/pykrx)가 해외 IP를 차단하면 데이터가 특정 날짜에
멈춘다. GitHub Actions는 미국 IP라 이 차단에 걸린다. 그래서 네이버 금융의
공개 시세 API를 대체 경로로 둔다.

핵심 원칙: 이 모듈은 KRX와 '똑같은 모양'의 데이터를 돌려준다.
반환 컬럼은 [code, date, open, high, low, close, volume, value]로,
_normalize_ohlcv가 소화할 수 있는 한글 컬럼(시가/고가/저가/종가/거래량)을
그대로 쓴다. 그래야 나머지 코드를 한 줄도 안 건드리고 소스만 갈아끼운다.

네이버 응답 형식(siseJson.naver, requestType=1):
  [['날짜','시가','고가','저가','종가','거래량','외국인소진율'],
   ["20260731", 71900, 72500, 71000, 72000, 1234567, 51.2],
   ...]
JSON이 아니라 파이썬 리터럴에 가까운 텍스트라 ast로 안전하게 파싱한다.
"""
from __future__ import annotations

import ast
import logging
import urllib.request

import pandas as pd

log = logging.getLogger(__name__)

_BASE = "https://api.finance.naver.com/siseJson.naver"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0 Safari/537.36"),
    "Referer": "https://finance.naver.com/",
}


def fetch_ohlcv_one(code: str, start: str, end: str,
                    timeout: float = 15.0) -> pd.DataFrame:
    """
    네이버에서 한 종목 일봉을 받아 KRX와 동일한 형태로 돌려준다.

    start, end: 'YYYY-MM-DD' 또는 'YYYYMMDD' 모두 허용.
    실패하면 빈 DataFrame을 돌려준다(호출부가 개별 실패를 건너뛰도록).
    """
    s = start.replace("-", "")
    e = end.replace("-", "")
    url = (f"{_BASE}?symbol={code}&requestType=1"
           f"&startTime={s}&endTime={e}&timeframe=day")

    try:
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        log.debug("네이버 응답 실패 %s: %s", code, exc)
        return pd.DataFrame()

    rows = _parse(text)
    if not rows or len(rows) < 2:
        return pd.DataFrame()

    header, *data = rows
    # 헤더 위치를 이름으로 매핑(순서가 바뀌어도 안전하게)
    idx = {name: i for i, name in enumerate(header)}
    need = ["날짜", "시가", "고가", "저가", "종가", "거래량"]
    if not all(k in idx for k in need):
        # 형식이 예상과 다르면 위치 기반으로 폴백(표준 순서 가정)
        idx = {"날짜": 0, "시가": 1, "고가": 2, "저가": 3, "종가": 4, "거래량": 5}

    recs = []
    for r in data:
        try:
            recs.append({
                "날짜": str(r[idx["날짜"]]),
                "시가": r[idx["시가"]],
                "고가": r[idx["고가"]],
                "저가": r[idx["저가"]],
                "종가": r[idx["종가"]],
                "거래량": r[idx["거래량"]],
            })
        except (IndexError, KeyError, TypeError):
            continue

    if not recs:
        return pd.DataFrame()

    df = pd.DataFrame(recs)
    df["날짜"] = pd.to_datetime(df["날짜"], format="%Y%m%d", errors="coerce")
    df = df.dropna(subset=["날짜"]).set_index("날짜")
    return df


def _parse(text: str) -> list:
    """네이버 응답 텍스트를 파이썬 리스트로. JSON이 아니라 ast로 판다."""
    if not text or text[0] != "[":
        return []
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        # 혹시 모를 형식 흔들림에 대비해 한 줄씩 시도
        try:
            import json
            return json.loads(text)
        except Exception:  # noqa: BLE001
            return []


def probe(code: str = "005930") -> bool:
    """네이버 접근 가능 여부를 빠르게 확인(삼성전자로 핑)."""
    df = fetch_ohlcv_one(code, "20200101", "20200110")
    return not df.empty
