"""수급 필터.

미너비니 원안에는 없는 항목입니다. 미국은 13F가 분기 단위라 실시간
기관 수급을 볼 수 없지만, 한국은 일별 투자자별 매매동향이 공개됩니다.
쓸 수 있는 정보를 안 쓸 이유가 없습니다.

기관 + 외국인 20일 누적 순매수가 양수인지를 봅니다.
개인만 사고 기관·외국인이 파는 상승은 대체로 오래 못 갑니다.
"""
from __future__ import annotations

import pandas as pd

EOK = 100_000_000


def summarize(flow_sums: pd.DataFrame, cfg) -> pd.DataFrame:
    """
    Parameters
    ----------
    flow_sums : code, inst_net, frgn_net, retail_net (기간 누적, 원 단위)

    Returns
    -------
    code를 인덱스로 하는 DataFrame:
        inst_eok, frgn_eok, smart_eok, flow_pass
    """
    if flow_sums is None or flow_sums.empty:
        return pd.DataFrame(columns=["inst_eok", "frgn_eok", "smart_eok", "flow_pass"])

    df = flow_sums.copy().set_index("code")
    for col in ("inst_net", "frgn_net", "retail_net"):
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    out = pd.DataFrame(index=df.index)
    out["inst_eok"] = (df["inst_net"] / EOK).round(1)
    out["frgn_eok"] = (df["frgn_net"] / EOK).round(1)
    out["smart_eok"] = ((df["inst_net"] + df["frgn_net"]) / EOK).round(1)

    if cfg.get("flows.require_net_positive", True):
        out["flow_pass"] = out["smart_eok"] > 0
    else:
        out["flow_pass"] = True

    return out
