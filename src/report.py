"""결과 출력.

latest.json 하나와 날짜별 히스토리를 씁니다. 프론트는 정적 파일만 읽으므로
서버가 필요 없습니다. GitHub Pages에 그대로 올라갑니다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))


def write(result: dict, cfg) -> Path:
    result = dict(result)
    result["generated_at"] = datetime.now(KST).isoformat(timespec="seconds")

    latest = cfg.path("output.json_path", "docs/data/latest.json")
    latest.parent.mkdir(parents=True, exist_ok=True)
    _dump(result, latest)

    hist_dir = cfg.path("output.history_dir", "docs/data/history")
    hist_dir.mkdir(parents=True, exist_ok=True)
    _dump(result, hist_dir / f"{result['as_of']}.json")

    _write_index(hist_dir, cfg)
    _prune(hist_dir, cfg.get("output.keep_history_days", 120))
    _append_internals(result, cfg)

    log.info("결과 기록: %s (1단계 %d, 2단계 %d)",
             latest, result["stats"]["stage1"], result["stats"]["stage2"])
    return latest


def _append_internals(result: dict, cfg) -> None:
    """
    시장 내부 지표를 한 줄씩 누적합니다.

    스냅샷만으로는 방향을 알 수 없습니다. 200일선 위 비율과 1단계 통과 수를
    시계열로 쌓아야 국면 전환을 읽을 수 있습니다. 시장 폭은 지수보다 먼저
    돌아서므로 바닥·천장 신호로 쓰입니다.
    """
    path = cfg.path("output.json_path").parent / "internals.json"
    series = []
    if path.exists():
        try:
            series = json.loads(path.read_text(encoding="utf-8")).get("series", [])
        except Exception:  # noqa: BLE001
            series = []

    r = result["regime"]
    s = result["stats"]
    point = {
        "date": result["as_of"],
        "breadth": r.get("breadth_above_ma200"),
        "stage1": s.get("stage1"),
        "stage2": s.get("stage2"),
        "breakout": s.get("breakout"),
        "verdict": r.get("verdict"),
    }
    # 같은 날짜는 갱신, 아니면 추가
    series = [p for p in series if p.get("date") != point["date"]]
    series.append(point)
    series.sort(key=lambda p: p["date"])
    series = series[-cfg.get("output.keep_history_days", 120):]

    _dump({"series": series}, path)


def _dump(obj: dict, path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _write_index(hist_dir: Path, cfg) -> None:
    """프론트의 날짜 선택기가 읽을 목록입니다."""
    dates = sorted(
        (p.stem for p in hist_dir.glob("*.json") if p.stem != "index"),
        reverse=True,
    )
    _dump({"dates": dates}, hist_dir / "index.json")


def _prune(hist_dir: Path, keep: int) -> None:
    files = sorted(
        (p for p in hist_dir.glob("*.json") if p.stem != "index"),
        key=lambda p: p.stem,
        reverse=True,
    )
    for p in files[keep:]:
        p.unlink(missing_ok=True)


def summary_text(result: dict) -> str:
    """콘솔 및 Actions 로그용 요약. 텔레그램 알림에 그대로 써도 됩니다."""
    r, s = result["regime"], result["stats"]
    verdict = {
        "risk_on": "정상 진입", "neutral": "중립",
        "caution": "선별 진입", "risk_off": "관망",
    }.get(r["verdict"], r["verdict"])

    lines = [
        f"[{result['as_of']}] 미너비니 스크리너",
        f"시장 국면: {verdict} | 200일선 위 {r['breadth_above_ma200']}%",
        f"유니버스 {s['universe']} → 1단계 {s['stage1']} → 워치리스트 {s['stage2']}"
        f" (돌파 {s['breakout']})",
    ]

    if result["stage2"]:
        lines.append("")
        lines.append("워치리스트 상위")
        for row in result["stage2"][:10]:
            v = row["vcp"]
            tag = "돌파" if row["signal"] == "breakout" else "셋업"
            dist = v.get("dist_to_pivot_pct")
            dist_s = f"{dist:+.1f}%" if dist is not None else "-"
            lines.append(
                f"  {tag} {row['name']}({row['code']}) "
                f"RS {row['rs_rating']} | VCP {v['score']} | 피벗 {dist_s}"
            )
    else:
        lines.append("")
        lines.append("오늘 워치리스트 없음. 기다리는 것도 포지션입니다.")

    return "\n".join(lines)
