#!/usr/bin/env python3
"""
미너비니 KR 스크리너 - 엔트리포인트

  python run_screen.py --backfill      최초 1회. 3년치 전 종목 일봉을 받습니다 (15~30분).
  python run_screen.py                 매일 실행. 증분 갱신 후 스크리닝합니다.
  python run_screen.py --screen-only   데이터 갱신 없이 스크리닝만 다시 돌립니다.
  python run_screen.py --demo          네트워크 없이 합성 데이터로 전 과정을 검증합니다.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from src import data as data_mod
from src import report as report_mod
from src import screener
from src.config import load_config
from src.db import Store

ROOT_CFG_PATH = Path(__file__).resolve().parent / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("run")


# --------------------------------------------------------------------

def backfill(store: Store, cfg) -> None:
    """
    최초 적재.

    설계상 세 가지를 지킵니다.
      1. 다운로드 전에 유니버스를 줄입니다. 어차피 탈락할 종목을 받지 않습니다.
      2. 150종목마다 DB에 씁니다. 중간에 끊겨도 받은 건 남습니다.
      3. 이미 받은 종목은 건너뜁니다. 다시 실행하면 이어받습니다.
    """
    markets = cfg.get("universe.markets", ["KOSPI", "KOSDAQ"])
    years = float(cfg.get("data.backfill_years", 2))
    start = (datetime.now() - timedelta(days=int(365.25 * years) + 30)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")

    log.info("종목 리스트 조회")
    try:
        listing = data_mod.fetch_listing(markets)
        store.upsert("listing", listing, ["code"])
        log.info("  %d종목", len(listing))
    except Exception as exc:  # noqa: BLE001
        # KRX가 종목 리스트 요청을 막는 일이 있습니다(Access Denied).
        # 리스트는 자주 바뀌지 않으므로 DB에 저장된 걸 재사용합니다.
        # 일봉 다운로드는 다른 경로라 리스트만 막혀도 백필은 계속됩니다.
        listing = store.listing_df()
        if listing.empty:
            log.error("종목 리스트를 받지 못했고 DB에도 없습니다.")
            raise
        log.warning("종목 리스트 조회 실패. DB에 저장된 %d종목으로 진행합니다.", len(listing))
        log.warning("  (KRX 일시 차단으로 보입니다. 최신 상장/폐지는 반영 안 될 수 있습니다.)")

    target = data_mod.prefilter_codes(listing, cfg)
    codes = target["code"].tolist()

    # --- 이미 받은 종목은 건너뜁니다 ---
    have = store.con.execute(
        "SELECT code, count(*) AS n, max(date) AS last FROM prices GROUP BY code"
    ).df()
    if not have.empty:
        min_rows = int(240 * years * 0.7)
        cutoff = (pd.Timestamp.today() - pd.Timedelta(days=12)).date()
        done_codes = set(
            have[(have["n"] >= min_rows) & (pd.to_datetime(have["last"]).dt.date >= cutoff)]["code"]
        )
        skip = [c for c in codes if c in done_codes]
        if skip:
            codes = [c for c in codes if c not in done_codes]
            log.info("  이미 받은 %d종목은 건너뜁니다. 남은 %d종목.", len(skip), len(codes))

    if not codes:
        log.info("모든 종목이 이미 적재되어 있습니다.")
    else:
        est = int(len(codes) / 5 / 60) + 1
        log.info("일봉 백필 %s ~ %s | %d종목 | 예상 %d~%d분",
                 start, end, len(codes), est, est * 3)
        log.info("중간에 끊겨도 받은 만큼은 저장됩니다. 다시 실행하면 이어받습니다.")

        written = {"n": 0}

        def save(chunk: pd.DataFrame) -> None:
            store.upsert("prices", chunk, ["code", "date"])
            written["n"] += len(chunk)

        data_mod.fetch_ohlcv_bulk(
            codes, start, end,
            max_workers=cfg.get("data.max_workers", 8),
            tries=cfg.get("data.retry", 3),
            sleep=cfg.get("data.retry_sleep_sec", 1.5),
            on_chunk=save,
            chunk_size=150,
        )
        log.info("  %d행 저장", written["n"])

    _refresh_side_tables(store, cfg, codes, full=True)
    store.set_meta("backfilled_at", datetime.now().isoformat())


def update(store: Store, cfg) -> None:
    """증분 갱신. 마지막 적재일 다음날부터 오늘까지 채웁니다."""
    markets = cfg.get("universe.markets", ["KOSPI", "KOSDAQ"])

    log.info("종목 리스트 갱신")
    try:
        listing = data_mod.fetch_listing(markets)
        store.upsert("listing", listing, ["code"])
    except Exception as exc:  # noqa: BLE001
        listing = store.listing_df()
        if listing.empty:
            log.error("종목 리스트를 받지 못했고 DB에도 없습니다.")
            raise
        log.warning("종목 리스트 조회 실패. DB의 %d종목으로 진행합니다.", len(listing))

    last = store.last_price_date()
    today = pd.Timestamp.today().normalize()

    if last is None:
        log.warning("기존 데이터가 없습니다. --backfill을 먼저 실행하세요.")
        return

    gap_days = (today - last).days
    log.info("마지막 적재일 %s (%d일 전)", last.date(), gap_days)

    if gap_days > 10:
        # 오래 안 돌렸으면 스냅샷을 날짜별로 도는 것보다 종목별 재조회가 빠릅니다.
        start = (last - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        log.info("공백이 커서 종목별로 재조회합니다 (%s ~ %s)", start, end)
        prices = data_mod.fetch_ohlcv_bulk(
            listing["code"].tolist(), start, end,
            max_workers=cfg.get("data.max_workers", 8),
        )
        store.upsert("prices", prices, ["code", "date"])
        log.info("  %d행", len(prices))
    else:
        cursor = last + pd.Timedelta(days=1)
        total = 0
        snapshot_failed = False
        while cursor <= today:
            if cursor.weekday() < 5:
                ymd = cursor.strftime("%Y%m%d")
                snap = data_mod.fetch_daily_snapshot(ymd, markets)
                if not snap.empty:
                    store.upsert("prices", snap, ["code", "date"])
                    total += len(snap)
                    log.info("  %s  %d종목", cursor.date(), len(snap))
                else:
                    snapshot_failed = True
            cursor += pd.Timedelta(days=1)

        # KRX 스냅샷이 한 번도 안 들어왔다 → 차단 가능성.
        # 종목별 재조회 경로(네이버 폴백 포함)로 공백을 메운다.
        if total == 0 and snapshot_failed:
            start = (last - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
            end = today.strftime("%Y-%m-%d")
            log.warning("KRX 스냅샷이 비었습니다. 종목별 재조회로 대체합니다 (%s ~ %s)", start, end)
            prices = data_mod.fetch_ohlcv_bulk(
                listing["code"].tolist(), start, end,
                max_workers=cfg.get("data.max_workers", 8),
            )
            if not prices.empty:
                store.upsert("prices", prices, ["code", "date"])
                total = len(prices)
        log.info("증분 %d행", total)

    _refresh_side_tables(store, cfg, listing["code"].tolist(), full=False)


def _refresh_side_tables(store: Store, cfg, codes: list[str], full: bool) -> None:
    """재무지표, 수급, 지수를 갱신합니다."""
    markets = cfg.get("universe.markets", ["KOSPI", "KOSDAQ"])
    as_of = data_mod.recent_business_day()

    log.info("재무지표 조회")
    fund = data_mod.fetch_fundamentals(as_of, markets)
    store.upsert("fundamentals", fund, ["code", "date"])
    log.info("  %d행", len(fund))

    log.info("지수 조회")
    start = "2018-01-01" if full else (
        datetime.now() - timedelta(days=400)).strftime("%Y%m%d")
    for code in (cfg.get("regime.kospi_code", "1001"),
                 cfg.get("regime.kosdaq_code", "2001")):
        idx = data_mod.fetch_index(code, start.replace("-", ""), as_of)
        store.upsert("indices", idx, ["code", "date"])

    # 수급은 종목별 호출이라 비쌉니다. 유동성 상위 종목만 받습니다.
    # 어차피 1단계를 통과하는 종목은 대부분 이 안에 들어옵니다.
    window = cfg.get("flows.window", 20)
    flow_start = (datetime.now() - timedelta(days=int(window * 2.2))).strftime("%Y%m%d")
    target = _liquid_codes(store, cfg, limit=700)
    log.info("수급 조회 %d종목", len(target))
    fl = data_mod.fetch_flows_bulk(target, flow_start, as_of)
    store.upsert("flows", fl, ["code", "date"])
    log.info("  %d행", len(fl))


def _liquid_codes(store: Store, cfg, limit: int = 1200) -> list[str]:
    """최근 20일 거래대금 상위 종목만 추립니다."""
    start = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    df = store.con.execute(
        """
        SELECT code, avg(value) AS v
        FROM prices WHERE date >= ?
        GROUP BY code ORDER BY v DESC LIMIT ?
        """,
        [start, limit],
    ).df()
    return df["code"].tolist()


# --------------------------------------------------------------------

def show_status(cfg) -> int:
    """지금 무엇이 적재돼 있고, 화면에 뜨는 게 진짜인지 가짜인지 알려줍니다."""
    import json

    print()
    print("=" * 52)
    print(" 현재 상태")
    print("=" * 52)

    # --- 1. SSL 준비 상태 ---
    import os
    cert = os.environ.get("SSL_CERT_FILE")
    if cert and os.path.exists(cert):
        print(f" [v] SSL 인증서  정상")
    else:
        print(" [x] SSL 인증서  미설정 — 이 폴더가 구버전입니다. 새 zip으로 교체하세요.")

    # --- 2. DB ---
    db_path = cfg.path("data.db_path", "data/market.duckdb")
    if not db_path.exists():
        print(" [x] 실제 데이터  없음 — 메뉴 2번(최초 데이터 적재)을 실행하세요.")
    else:
        with Store(db_path) as store:
            rows = store.row_count("prices")
            codes = store.con.execute(
                "SELECT count(DISTINCT code) FROM prices").fetchone()[0]
            last = store.last_price_date()
            listed = store.con.execute("SELECT count(*) FROM listing").fetchone()[0]
            if rows == 0:
                print(" [x] 실제 데이터  DB는 있으나 비어 있음 — 2번을 다시 실행하세요.")
            else:
                print(f" [v] 실제 데이터  {codes:,}종목 / {rows:,}행 / 최종 {last.date()}")
                # 적재가 도중에 멈췄는지 가늠합니다.
                if listed and codes < listed * 0.35:
                    print(f"                 아직 적재 중이거나 중단된 것 같습니다.")
                    print(f"                 2번을 다시 실행하면 받은 지점부터 이어받습니다.")

    # --- 3. 화면에 뜨는 결과 ---
    latest = cfg.path("output.json_path", "docs/data/latest.json")
    if not latest.exists():
        print(" [x] 화면 결과    없음")
    else:
        try:
            d = json.loads(latest.read_text(encoding="utf-8"))
            if d.get("demo"):
                print(f" [!] 화면 결과    데모(가짜) 데이터 — 기준일 {d.get('as_of')}")
                print("                 2번을 성공적으로 마치면 실제 데이터로 바뀝니다.")
            else:
                s = d.get("stats", {})
                print(f" [v] 화면 결과    실제 데이터 — 기준일 {d.get('as_of')}, "
                      f"1단계 {s.get('stage1')}종목")
        except Exception as exc:  # noqa: BLE001
            print(f" [x] 화면 결과    파일이 손상됨: {exc}")

    print("=" * 52)
    print()
    return 0


def _apply_params(cfg, params: dict) -> None:
    """
    그리드 서치 1등 조합을 config.yaml에 씁니다.

    YAML 구조를 통째로 다시 쓰면 주석이 날아갑니다. 주석은 이 파일의
    핵심 자산(각 값이 뭘 의미하는지)이라 반드시 보존해야 합니다.
    그래서 해당 줄만 정규식으로 찾아 값 부분만 교체합니다.
    """
    import re
    import shutil

    cfg_path = ROOT_CFG_PATH
    shutil.copy(cfg_path, str(cfg_path) + ".bak")

    lines = cfg_path.read_text(encoding="utf-8").splitlines(keepends=True)

    for dotted, value in params.items():
        # "vcp.max_last_depth" → 마지막 키 "max_last_depth"
        leaf = dotted.split(".")[-1]
        # bool은 소문자 유지, 숫자는 그대로
        if isinstance(value, bool):
            val_str = "true" if value else "false"
        elif isinstance(value, float) and value.is_integer():
            val_str = str(int(value))
        else:
            val_str = str(value)

        pat = re.compile(rf"^(\s+{re.escape(leaf)}:\s*)([^\s#]+)(.*)$")
        for i, line in enumerate(lines):
            m = pat.match(line)
            if m:
                lines[i] = f"{m.group(1)}{val_str}{m.group(3)}\n"
                break

    cfg_path.write_text("".join(lines), encoding="utf-8")


def run_backtest(cfg, args) -> int:
    """백테스트 실행. 단일 실행 또는 파라미터 그리드 서치."""
    import json
    from src import backtest as bt_mod

    db_path = cfg.path("data.db_path", "data/market.duckdb")
    if not db_path.exists():
        log.error("데이터가 없습니다. 먼저 --backfill을 실행하세요.")
        return 1

    with Store(db_path) as store:
        repaired = store.repair_values()
        if repaired:
            log.info("거래대금 %d행 보정", repaired)

        bt = bt_mod.Backtester(store, cfg, start=args.start, end=args.end)

        if args.grid:
            grid = {
                # 국면 필터: 시장 폭이 이 아래면 신규 진입 중단.
                # 백테스트에서 '정상' 국면 진입이 대량 손실을 냈으므로
                # 기준을 조여서 나쁜 국면을 더 걸러내는지 본다.
                "regime.breadth_risk_off": [0.25, 0.35, 0.45],
                # 손절 허용 폭: 이보다 손절이 멀면 진입 자체를 포기.
                # 현재 손절 평균 -9.45%가 이익을 갉아먹고 있다.
                "backtest.max_stop_distance_pct": [7, 9, 12],
                # 본전 스톱을 언제 올리는가. 빨리 올릴수록 손실은 줄지만
                # 정상적인 흔들림에 조기 이탈할 위험도 커진다.
                "backtest.breakeven_at_pct": [6, 8, 12],
                # RS 문턱. 선도주만 노릴지.
                "trend_template.min_rs_rating": [70, 85],
            }
            table = bt_mod.grid_search(bt, grid, top=20)
            print()
            print("=" * 100)
            print(" 그리드 서치 상위 조합 (기대값 R 기준 정렬)")
            print("=" * 100)
            print(table.to_string(index=False))
            print()
            out = cfg.path("output.json_path").parent / "backtest_grid.csv"
            table.to_csv(out, index=False, encoding="utf-8-sig")
            print(f" 전체 결과: {out}")

            # 1등 조합을 판정과 함께 안내
            if not table.empty:
                best = table.iloc[0]
                param_cols = [c for c in table.columns if "." in c]
                print()
                print("-" * 100)
                if best["expectancy_r"] <= 0:
                    print(" 최고 조합조차 기대값이 0 이하입니다.")
                    print(" 이 전략은 현재 시장 구간에서 작동하지 않습니다. 파라미터가 아니라")
                    print(" 접근 자체를 재검토해야 합니다. 억지로 맞추지 마세요.")
                elif best["trades"] < 20:
                    print(f" 1등 조합의 거래가 {int(best['trades'])}건뿐입니다. 통계적으로 얇습니다.")
                    print(" 참고만 하고 실매매 반영은 보류하세요.")
                else:
                    print(" 추천 조합 (config.yaml에 반영):")
                    for c in param_cols:
                        print(f"   {c}: {best[c]}")
                    print()
                    print(f" 기대값 {best['expectancy_r']:+.3f}R · 거래 {int(best['trades'])}건 "
                          f"· 승률 {best['win_rate']}% · 총수익 {best['total_return_pct']:+.1f}%")
                    print()
                    print(" --apply 를 붙여 실행하면 이 값을 config.yaml에 자동으로 씁니다.")
                    if args.apply:
                        _apply_params(cfg, {c: best[c] for c in param_cols})
                        print()
                        print(" [적용됨] config.yaml을 갱신했습니다. 백업은 config.yaml.bak 입니다.")
                print("-" * 100)
            return 0

        result = bt.run(progress=True)
        print()
        print(bt_mod.report_text(result))

        # 결과 저장
        out_dir = cfg.path("output.json_path").parent
        out_dir.mkdir(parents=True, exist_ok=True)

        tdf = result.trades_df()
        if not tdf.empty:
            csv_path = out_dir / "backtest_trades.csv"
            tdf.to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"\n 거래 내역: {csv_path}")

        payload = {
            "metrics": result.metrics,
            "equity": [
                {"date": d.strftime("%Y-%m-%d"), "value": round(v)}
                for d, v in result.equity.items()
            ],
            "trades": tdf.to_dict("records") if not tdf.empty else [],
        }
        json_path = out_dir / "backtest.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False)
        print(f" 요약 JSON: {json_path}")

    return 0


def _reason_bucket(reason: str) -> str:
    """탈락 사유에서 개별 수치를 떼어내 유형으로 묶습니다."""
    for key, label in (
        ("베이스", "베이스 길이 범위 밖"),
        ("수축폭", "수축폭이 줄지 않음"),
        ("첫 조정", "첫 조정이 너무 깊음"),
        ("마지막 조정", "마지막 조정이 너무 깊음"),
        ("거래량 미수축", "거래량이 함께 줄지 않음"),
        ("거래량 고갈", "돌파 직전 거래량 고갈 부족"),
        ("피벗까지", "저항선에서 아직 멂"),
        ("연장", "이미 연장됨 (늦음)"),
        ("스윙", "스윙이 안 잡힘"),
        ("수축", "수축 횟수 부족"),
        ("데이터", "데이터 부족"),
        ("VCP 성립", "VCP 성립"),
    ):
        if key in reason:
            return label
    return reason


def show_why(cfg) -> int:
    """
    1단계를 통과한 종목이 왜 셋업이 못 됐는지 종목별로 보여줍니다.

    셋업 0이 버그인지 시장 상황인지 구분하는 용도입니다. 탈락 사유가
    한쪽에 몰려 있으면 그 임계값이 과하게 조인 것이고, 사유가 흩어져
    있으면 실제로 베이스를 만드는 종목이 없는 것입니다.
    """
    import collections

    from src import screener, vcp as vcp_mod

    db_path = cfg.path("data.db_path", "data/market.duckdb")
    if not db_path.exists():
        log.error("데이터가 없습니다. 먼저 --backfill을 실행하세요.")
        return 1

    with Store(db_path) as store:
        store.repair_values()
        result = screener.run(store, cfg)

        prices = store.price_panel()
        prices["date"] = pd.to_datetime(prices["date"])

        rows = result["stage1"]
        print()
        print("=" * 74)
        print(f" 1단계 통과 {len(rows)}종목의 VCP 판정")
        print("=" * 74)

        reasons = collections.Counter()
        for r in rows:
            one = prices[prices["code"] == r["code"]].sort_values("date")
            res = vcp_mod.detect(one, cfg)
            reasons[_reason_bucket(res.reason)] += 1
            mark = "O" if res.detected else " "
            depth = ""
            if res.contractions:
                depth = " [" + " → ".join(
                    f"{c.depth_pct:.0f}%" for c in res.contractions) + "]"
            print(f" {mark} {r['name'][:12]:<13s} RS{r['rs_rating']:>3}  "
                  f"고점대비{r['from_high_pct']:>6.1f}%  {res.reason}{depth}")

        print()
        print("-" * 74)
        print(" 탈락 사유 집계")
        print("-" * 74)
        for reason, n in reasons.most_common():
            print(f"  {n:3d}  {reason}")

        print()
        print("-" * 74)
        top = reasons.most_common(1)[0][0] if reasons else ""
        advice = {
            "이미 연장됨 (늦음)":
                ("대부분 이미 크게 뻗었습니다. 늦게 들어가지 말라는 신호입니다.\n"
                 " 그래도 보시려면 vcp.max_extended_above_pivot을 올리세요."),
            "수축폭이 줄지 않음":
                ("조정폭이 계단식으로 줄지 않습니다. 베이스가 아직 어수선합니다.\n"
                 " 정상적인 탈락입니다. 억지로 완화하면 가짜 신호가 늘어납니다."),
            "저항선에서 아직 멂":
                "저항선에서 멉니다. vcp.max_dist_to_pivot을 키우면 더 일찍 잡힙니다.",
            "베이스 길이 범위 밖":
                ("베이스가 짧습니다. 조정 기간이 아직 안 찼다는 뜻입니다.\n"
                 " 더 이른 단계까지 보시려면 vcp.min_base_days를 12 정도로 낮추세요."),
            "거래량이 함께 줄지 않음":
                ("수축은 하는데 거래량이 안 따라 줄었습니다. 매물 소진이 덜 된 상태입니다.\n"
                 " 완화하려면 vcp.max_volume_ratio를 0.9로 올리세요."),
            "돌파 직전 거래량 고갈 부족":
                "거래량 고갈이 약합니다. vcp.max_dryup_ratio를 0.95로 올려보세요.",
            "스윙이 안 잡힘":
                ("가격이 밋밋해 수축 자체가 안 잡힙니다.\n"
                 " vcp.swing_threshold를 0.04로 낮춰보세요."),
            "수축 횟수 부족":
                "수축이 2회도 안 나옵니다. vcp.swing_threshold를 낮춰보세요.",
        }.get(top, "사유가 흩어져 있습니다. 실제로 베이스를 만드는 종목이 없는 국면입니다.")
        print(f" {advice}")
        print("=" * 74)
        print()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="미너비니 KR 스크리너")
    ap.add_argument("--backfill", action="store_true", help="최초 전체 적재")
    ap.add_argument("--screen-only", action="store_true", help="데이터 갱신 없이 스크리닝만")
    ap.add_argument("--demo", action="store_true", help="합성 데이터로 파이프라인 검증")
    ap.add_argument("--status", action="store_true", help="현재 적재 상태 확인")
    ap.add_argument("--backtest", action="store_true", help="백테스트 실행")
    ap.add_argument("--why", action="store_true",
                    help="1단계 통과 종목이 왜 셋업이 못 됐는지 진단")
    ap.add_argument("--grid", action="store_true", help="파라미터 그리드 서치")
    ap.add_argument("--apply", action="store_true",
                    help="그리드 서치 1등 조합을 config.yaml에 자동 반영")
    ap.add_argument("--start", default=None, help="백테스트 시작일 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="백테스트 종료일 YYYY-MM-DD")
    ap.add_argument("--config", default=None, help="설정 파일 경로")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.status:
        return show_status(cfg)

    if args.why:
        return show_why(cfg)

    if args.backtest or args.grid:
        return run_backtest(cfg, args)

    if args.demo:
        from tests.make_demo import build_demo
        build_demo(cfg)
        return 0

    db_path = cfg.path("data.db_path", "data/market.duckdb")
    with Store(db_path) as store:
        try:
            if args.backfill:
                backfill(store, cfg)
            elif not args.screen_only:
                update(store, cfg)
        except Exception as exc:  # noqa: BLE001
            log.error("데이터 갱신 실패: %s", exc)
            if store.row_count("prices") == 0:
                log.error("")
                log.error("적재된 데이터가 없어 중단합니다.")
                log.error("화면에는 이전 결과가 그대로 남아 있습니다. 위 오류를 먼저 해결하세요.")
                return 1
            log.warning("기존 데이터로 스크리닝을 계속합니다. 최신 시세가 아닐 수 있습니다.")

        admin = set()
        try:
            admin = data_mod.fetch_admin_issues(data_mod.recent_business_day())
        except Exception:  # noqa: BLE001
            pass

        result = screener.run(store, cfg, admin)
        report_mod.write(result, cfg)
        print()
        print(report_mod.summary_text(result))

    return 0


if __name__ == "__main__":
    sys.exit(main())
