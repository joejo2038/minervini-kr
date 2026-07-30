"""스크리닝 파이프라인.

    유니버스 정제
        ↓
    1단계  Trend Template (8조건)      → 관심종목 풀
        ↓
    필터   수급 + 재무
        ↓
    2단계  VCP 수축 탐지                → 워치리스트
        ↓
    분류   돌파(BUY) / 셋업(SETUP)
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from . import flows as flows_mod
from . import fundamentals as fund_mod
from . import indicators as ind
from . import internals as internals_mod
from . import regime as regime_mod
from . import trend_template as tt
from . import universe as uni
from . import vcp as vcp_mod

log = logging.getLogger(__name__)

EOK = 100_000_000


def run(store, cfg, admin_codes: set[str] | None = None) -> dict:
    # 거래대금이 비어 있으면 채웁니다. 안 하면 유동성 필터가 전 종목을 걸러냅니다.
    repaired = store.repair_values()
    if repaired:
        log.info("거래대금 %d행 보정 (종가 x 거래량)", repaired)

    listing = store.listing_df()
    if listing.empty:
        raise RuntimeError("종목 리스트가 비어 있습니다. 먼저 데이터를 적재하세요.")

    prices = store.price_panel()
    if prices.empty:
        raise RuntimeError("가격 데이터가 비어 있습니다.")

    prices["date"] = pd.to_datetime(prices["date"])
    as_of = prices["date"].max()
    log.info("기준일 %s | 전체 %d종목 %d행", as_of.date(), listing["code"].nunique(), len(prices))

    # ---------- 유니버스 ----------
    clean = uni.apply(listing, prices, cfg, admin_codes)
    codes = clean["code"].tolist()
    if not codes:
        raise RuntimeError(
            "유니버스가 비었습니다.\n"
            "  바로 위 로그에서 어떤 필터가 가장 많이 걸러냈는지 확인하세요.\n"
            "  config.yaml에서 해당 항목을 낮추면 됩니다.\n"
            "    거래대금 부족  → universe.min_avg_value_eok\n"
            "    시가총액 부족  → universe.min_marcap_eok\n"
            "    상장 기간 부족 → universe.min_listed_days"
        )

    sub = prices[prices["code"].isin(codes)]
    close_w = ind.to_wide(sub, "close")
    high_w = ind.to_wide(sub, "high")
    low_w = ind.to_wide(sub, "low")
    volume_w = ind.to_wide(sub, "volume")
    value_w = ind.to_wide(sub, "value")

    # ---------- RS Rating ----------
    raw = ind.rs_raw(close_w,
                     cfg.get("rs.periods", [63, 126, 189, 252]),
                     cfg.get("rs.weights", [0.4, 0.2, 0.2, 0.2]))
    groups = None
    if cfg.get("rs.rank_by_market", False):
        groups = clean.set_index("code")["market"]
    rs = ind.rs_rating(raw, groups)
    log.info("RS Rating 산출 %d종목", int(rs.notna().sum()))

    # ---------- 1단계: Trend Template ----------
    tt_df = tt.evaluate(close_w, high_w, low_w, rs, cfg)
    stage1_codes = tt_df.index[tt_df["tt_pass"]].tolist()
    log.info("1단계 통과 %d종목", len(stage1_codes))

    # 시장 폭: 전 종목 중 200일선 위 비율
    ma200 = close_w.rolling(200, min_periods=200).mean().iloc[-1]
    last_close = close_w.iloc[-1]
    valid = ma200.notna() & last_close.notna()
    breadth = float((last_close[valid] > ma200[valid]).mean()) if valid.any() else 0.0

    # ---------- 수급 / 재무 ----------
    window = cfg.get("flows.window", 20)
    flow_start = (as_of - pd.Timedelta(days=int(window * 1.7))).strftime("%Y-%m-%d")
    flow_tbl = flows_mod.summarize(store.flow_sums(flow_start), cfg)
    fund_tbl = fund_mod.basic_filter(store.latest_fundamentals(), cfg)
    growth_tbl = fund_mod.dart_growth(stage1_codes, cfg)

    # ---------- 2단계: VCP ----------
    meta = clean.set_index("code")
    avg_value = ind.average_value(value_w, 20)

    stage1_rows: list[dict] = []
    stage2_rows: list[dict] = []

    for code in stage1_codes:
        t = tt_df.loc[code]

        row = {
            "code": code,
            "name": str(meta.at[code, "name"]) if code in meta.index else code,
            "market": str(meta.at[code, "market"]) if code in meta.index else "",
            "close": _f(t["close"]),
            "chg_pct": _chg(close_w[code]),
            "marcap_eok": _i(meta.at[code, "marcap"] / EOK) if code in meta.index else None,
            "value_20d_eok": _f(avg_value.get(code, np.nan) / EOK, 1),
            "rs_rating": _i(t["rs_rating"]),
            "ma50": _f(t["ma50"]),
            "ma150": _f(t["ma150"]),
            "ma200": _f(t["ma200"]),
            "from_low_pct": _f(t["from_low_pct"], 1),
            "from_high_pct": _f(t["from_high_pct"], 1),
            "tt": {f"c{i}": bool(t[f"c{i}"]) for i in range(1, 9)},
            "tt_passed": int(t["passed"]),
        }

        # 수급
        if code in flow_tbl.index:
            f = flow_tbl.loc[code]
            row["flow"] = {
                "inst_eok": _f(f["inst_eok"], 1),
                "frgn_eok": _f(f["frgn_eok"], 1),
                "smart_eok": _f(f["smart_eok"], 1),
                "pass": bool(f["flow_pass"]),
            }
        else:
            row["flow"] = {"inst_eok": None, "frgn_eok": None,
                           "smart_eok": None, "pass": None}

        # 재무
        if code in fund_tbl.index:
            fu = fund_tbl.loc[code]
            row["fund"] = {
                "per": _f(fu["per"], 2),
                "pbr": _f(fu["pbr"], 2),
                "profitable": bool(fu["profitable"]),
                "pass": bool(fu["fund_pass"]),
            }
        else:
            row["fund"] = {"per": None, "pbr": None,
                           "profitable": None, "pass": None}

        if code in growth_tbl.index:
            row["fund"]["eps_growth_yoy"] = _f(growth_tbl.at[code, "eps_growth_yoy"], 1)
            row["fund"]["growth_pass"] = bool(growth_tbl.at[code, "growth_pass"])

        stage1_rows.append(row)

        # --- 하드 필터 적용 여부 판정 ---
        if cfg.get("flows.hard_filter", False) and row["flow"]["pass"] is False:
            continue
        if row["fund"]["pass"] is False:
            continue

        # --- VCP ---
        one = sub[sub["code"] == code].sort_values("date")
        result = vcp_mod.detect(one, cfg)
        if not result.detected:
            continue

        v = result.to_dict()
        # 최종 정렬 점수 = VCP 점수 70% + RS 30%
        rs_val = row["rs_rating"] or 0
        v["rank_score"] = round(v["score"] * 0.7 + rs_val * 0.3, 1)
        row2 = dict(row)
        row2["vcp"] = v
        row2["signal"] = "breakout" if v["breakout"] else "setup"
        row2["spark"] = _spark(close_w[code])
        stage2_rows.append(row2)

    stage1_rows.sort(key=lambda r: (r["rs_rating"] or 0), reverse=True)
    stage2_rows.sort(key=lambda r: r["vcp"]["rank_score"], reverse=True)

    max_rows = cfg.get("output.max_stage1_rows", 400)
    reg = regime_mod.evaluate(store, breadth, len(stage1_codes), len(codes), cfg)

    # 실제 캔들 차트용 OHLC. 1단계 통과 종목만, 최근 N일만 담습니다.
    # 전 종목을 한 파일에 넣으면 무거워지므로 클릭 시 지연 로딩할 수 있게
    # 종목별로 쪼갤 수도 있지만, 26개 수준이면 한 파일로 충분합니다.
    chart_days = cfg.get("output.chart_days", 160)
    charts = _build_charts(sub, [r["code"] for r in stage1_rows],
                           stage2_rows, days=chart_days)

    # 섹터 리더십: 업종별 RS 중앙값과 1단계 통과 분포
    sectors = _build_sectors(clean, rs, tt_df, stage1_rows)

    # 시장 내부 지표: 200/50일선 위 비율, 신고-신저, 1단계 통과 수의 소급 시계열
    try:
        rs_hist = ind.rs_rating_history(
            close_w,
            cfg.get("rs.periods", [63, 126, 189, 252]),
            cfg.get("rs.weights", [0.4, 0.2, 0.2, 0.2]),
        )
        internals = internals_mod.compute_series(
            close_w, high_w, low_w, rs_hist, cfg,
            tail_days=cfg.get("output.internals_days", 900),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("시장 내부 지표 계산 실패: %s", exc)
        internals = None

    return {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "regime": reg,
        "stats": {
            "listed": int(listing["code"].nunique()),
            "universe": len(codes),
            "stage1": len(stage1_rows),
            "stage2": len(stage2_rows),
            "breakout": sum(1 for r in stage2_rows if r["signal"] == "breakout"),
        },
        "stage1": stage1_rows[:max_rows],
        "stage2": stage2_rows,
        "charts": charts,
        "sectors": sectors,
        "internals": internals,
        "condition_labels": tt.CONDITION_LABELS,
    }


# --------------------------------------------------------------------
# 차트 · 섹터 빌드
# --------------------------------------------------------------------

def _build_charts(sub: pd.DataFrame, codes: list[str],
                  stage2_rows: list[dict], days: int = 160) -> dict:
    """
    1단계 종목의 최근 일봉을 실제 캔들 차트용으로 직렬화합니다.

    각 종목마다 OHLC + 이동평균 3개 + 거래량, 그리고 셋업이면 피벗·손절선을
    함께 담습니다. 프론트는 이걸로 진짜 차트를 그립니다.
    """
    vcp_by_code = {r["code"]: r.get("vcp", {}) for r in stage2_rows}
    charts: dict[str, dict] = {}

    for code in codes:
        one = sub[sub["code"] == code].sort_values("date").tail(days)
        if len(one) < 20:
            continue

        close = one["close"]
        ma50 = close.rolling(50, min_periods=1).mean()
        ma150 = close.rolling(150, min_periods=1).mean()
        ma200 = close.rolling(200, min_periods=1).mean()

        candles = []
        for i, (_, r) in enumerate(one.iterrows()):
            candles.append([
                _f(r["open"], 1), _f(r["high"], 1),
                _f(r["low"], 1), _f(r["close"], 1),
                _i(r["volume"]),
            ])

        chart = {
            "dates": [d.strftime("%Y-%m-%d") for d in pd.to_datetime(one["date"])],
            "candles": candles,
            "ma50": [_f(x, 1) for x in ma50],
            "ma150": [_f(x, 1) for x in ma150],
            "ma200": [_f(x, 1) for x in ma200],
        }

        v = vcp_by_code.get(code)
        if v:
            chart["pivot"] = v.get("pivot")
            chart["stop"] = v.get("stop_price")
        # 셋업이 아닌 종목에도 저항 기준선을 줍니다. 52주 고점.
        hi52 = one["high"].max()
        chart["hi52"] = _f(hi52, 1)
        charts[code] = chart

    return charts


# 업종 대분류 매핑. pykrx가 업종을 항상 주지는 않으므로, 종목명 키워드로
# 러프하게 분류하고 나머지는 '기타'로 둡니다. 정확한 업종 코드가 필요하면
# 나중에 KRX 업종 테이블을 붙일 수 있습니다.
_SECTOR_KEYWORDS = {
    "반도체": ["반도체", "전자", "SK하이닉스", "DB하이텍", "이오테크닉스", "한미반도체"],
    "2차전지": ["에너지솔루션", "배터리", "엘앤에프", "에코프로", "포스코퓨처엠", "SK아이이테크"],
    "자동차": ["모비스", "현대차", "기아", "만도", "타이어", "HL"],
    "바이오": ["바이오", "제약", "셀트리온", "메디", "genexine", "파마", "녹십자", "종근당"],
    "금융": ["금융", "은행", "증권", "보험", "화재", "카드", "캐피탈", "지주", "홀딩스"],
    "화학": ["화학", "케미칼", "석유", "S-Oil", "정유", "SKC"],
    "철강": ["철강", "포스코", "제철", "금속", "현대제철"],
    "조선": ["조선", "중공업", "해양", "HD한국조선"],
    "건설": ["건설", "산업개발", "엔지니어링", "GS건설", "대우건설"],
    "IT서비스": ["소프트", "게임", "엔터", "카카오", "네이버", "NAVER", "테크", "IT"],
    "소비재": ["글로벌", "식품", "화장품", "리테일", "유통", "생활건강", "아모레"],
    "기계": ["기계", "로봇", "중전기", "일렉트릭", "산전"],
}


def _sector_of(name: str) -> str:
    for sector, kws in _SECTOR_KEYWORDS.items():
        for kw in kws:
            if kw in name:
                return sector
    return "기타"


def _build_sectors(clean: pd.DataFrame, rs: pd.Series,
                   tt_df: pd.DataFrame, stage1_rows: list[dict]) -> list[dict]:
    """
    업종별 상대강도와 1단계 통과 분포.

    미너비니가 강조하는 '선도 그룹' 개념입니다. 자금이 어느 업종으로
    몰리는지 보이면 개별 종목보다 먼저 흐름을 읽을 수 있습니다.
    """
    df = clean[["code", "name"]].copy()
    df["sector"] = df["name"].map(_sector_of)
    df["rs"] = df["code"].map(rs.to_dict())
    df["tt_pass"] = df["code"].map(tt_df["tt_pass"].to_dict()).fillna(False)

    stage1_codes = {r["code"] for r in stage1_rows}
    df["in_stage1"] = df["code"].isin(stage1_codes)

    out = []
    for sector, g in df.groupby("sector"):
        rs_vals = g["rs"].dropna()
        if len(g) < 3:  # 표본 3개 미만 업종은 노이즈
            continue
        out.append({
            "sector": sector,
            "count": len(g),
            "rs_median": _i(rs_vals.median()) if len(rs_vals) else None,
            "rs_top": _i(rs_vals.max()) if len(rs_vals) else None,
            "stage1_count": int(g["in_stage1"].sum()),
            "stage1_pct": round(g["in_stage1"].sum() / len(g) * 100, 1),
        })

    # RS 중앙값 높은 순 = 자금이 몰리는 업종 순
    out.sort(key=lambda x: (x["rs_median"] or 0), reverse=True)
    return out


# --------------------------------------------------------------------
# 직렬화 헬퍼 — JSON에 NaN이 들어가면 프론트가 깨집니다
# --------------------------------------------------------------------

def _f(v, nd: int = 0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(f):
        return None
    return round(f, nd) if nd else round(f)


def _i(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(round(f)) if np.isfinite(f) else None


def _chg(series: pd.Series):
    s = series.dropna()
    if len(s) < 2 or s.iloc[-2] == 0:
        return None
    return round((s.iloc[-1] / s.iloc[-2] - 1) * 100, 2)


def _spark(series: pd.Series, n: int = 60) -> list[float]:
    """프론트 미니차트용. 0~1로 정규화해서 60포인트만 보냅니다."""
    s = series.dropna().tail(n)
    if len(s) < 5:
        return []
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        return [0.5] * len(s)
    return [round((float(x) - lo) / (hi - lo), 3) for x in s]
