import sys, itertools, pandas as pd
sys.path.insert(0, '.')
from src.config import load_config
from src.db import Store
from src import screener, vcp as V

class Ov:
    def __init__(s, b, o): s.b, s.o = b, o
    def get(s, p, d=None): return s.o.get(p, s.b.get(p, d))

cfg = load_config()
with Store(cfg.path("data.db_path")) as st:
    st.repair_values()
    rows = screener.run(st, cfg)["stage1"]
    px = st.price_panel(); px["date"] = pd.to_datetime(px["date"])
    series = {r["code"]: px[px["code"] == r["code"]].sort_values("date") for r in rows}

print(f"\n1단계 통과 {len(rows)}종목 대상으로 조합 비교\n")
print(f"{'스윙':>6} {'최소베이스':>10} {'셋업':>6}   상위 종목")
print("-" * 68)
best = []
for thr, mb in itertools.product([0.05, 0.06, 0.07, 0.08], [15, 12, 10]):
    ov = Ov(cfg, {"vcp.swing_threshold": thr, "vcp.min_base_days": mb})
    hits = []
    for r in rows:
        v = V.detect(series[r["code"]], ov)
        if v.detected:
            hits.append((v.score, r["name"], v.dist_to_pivot_pct))
    hits.sort(reverse=True)
    names = ", ".join(f"{n}({d:+.1f}%)" for _, n, d in hits[:3])
    print(f"{thr:>6.2f} {mb:>10} {len(hits):>6}   {names}")
    best.append((len(hits), thr, mb))

print("-" * 68)
ok = [b for b in best if 2 <= b[0] <= 8]
if ok:
    n, thr, mb = max(ok)
    print(f"\n추천: swing_threshold {thr}, min_base_days {mb}  (셋업 {n}개)")
    print("config.yaml의 vcp 항목에서 두 줄을 이 값으로 바꾸세요.")
else:
    print("\n적정 구간(2~8개)이 없습니다. 시장이 정말 비어 있는 국면입니다.")
print()
