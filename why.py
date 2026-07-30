import collections, sys, pandas as pd
sys.path.insert(0, '.')
from src.config import load_config
from src.db import Store
from src import screener, vcp as V

BUCKET = [("베이스","베이스 길이 범위 밖"),("수축폭","수축폭이 줄지 않음"),
          ("첫 조정","첫 조정이 너무 깊음"),("마지막 조정","마지막 조정이 너무 깊음"),
          ("거래량 미수축","거래량이 함께 줄지 않음"),("거래량 고갈","돌파 직전 거래량 고갈 부족"),
          ("피벗까지","저항선에서 아직 멂"),("연장","이미 연장됨 (늦음)"),
          ("스윙","스윙이 안 잡힘"),("수축","수축 횟수 부족"),("데이터","데이터 부족"),
          ("VCP 성립","VCP 성립")]
def bucket(r):
    for k, l in BUCKET:
        if k in r: return l
    return r

cfg = load_config()
with Store(cfg.path("data.db_path")) as st:
    st.repair_values()
    res = screener.run(st, cfg)
    px = st.price_panel(); px["date"] = pd.to_datetime(px["date"])
    rows = res["stage1"]
    print("\n" + "="*74)
    print(f" 1단계 통과 {len(rows)}종목의 VCP 판정")
    print("="*74)
    cnt = collections.Counter()
    for r in rows:
        one = px[px["code"] == r["code"]].sort_values("date")
        v = V.detect(one, cfg)
        cnt[bucket(v.reason)] += 1
        d = " [" + " → ".join(f"{c.depth_pct:.0f}%" for c in v.contractions) + "]" if v.contractions else ""
        print(f" {'O' if v.detected else ' '} {r['name'][:12]:<13s} RS{r['rs_rating']:>3}  "
              f"고점대비{r['from_high_pct']:>6.1f}%  {v.reason}{d}")
    print("\n" + "-"*74 + "\n 탈락 사유 집계\n" + "-"*74)
    for k, n in cnt.most_common(): print(f"  {n:3d}  {k}")
    top = cnt.most_common(1)[0][0] if cnt else ""
    tip = {"이미 연장됨 (늦음)":"이미 크게 뻗었습니다. 늦게 들어가지 말라는 신호입니다.",
      "수축폭이 줄지 않음":"베이스가 아직 어수선합니다. 정상적인 탈락입니다.",
      "저항선에서 아직 멂":"vcp.max_dist_to_pivot을 키우면 더 일찍 잡힙니다.",
      "베이스 길이 범위 밖":"조정 기간이 덜 찼습니다. vcp.min_base_days를 12로 낮춰보세요.",
      "거래량이 함께 줄지 않음":"매물 소진이 덜 됐습니다. vcp.max_volume_ratio를 0.9로 올려보세요.",
      "돌파 직전 거래량 고갈 부족":"vcp.max_dryup_ratio를 0.95로 올려보세요.",
      "스윙이 안 잡힘":"vcp.swing_threshold를 0.04로 낮춰보세요.",
      "수축 횟수 부족":"vcp.swing_threshold를 0.04로 낮춰보세요."}.get(top,
      "사유가 흩어져 있습니다. 실제로 베이스를 만드는 종목이 없는 국면입니다.")
    print("\n" + "-"*74 + f"\n {tip}\n" + "="*74 + "\n")
