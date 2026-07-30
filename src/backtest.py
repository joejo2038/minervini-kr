"""
백테스트 엔진.

config.yaml의 임계값은 전부 추정치입니다. 검증 없이 실매매에 쓰는 건
위험하므로, 과거 데이터로 신호를 재현하고 결과를 측정합니다.

미래 정보 누출 차단
-------------------
백테스트가 거짓말을 하는 가장 흔한 경로입니다. 두 가지로 막습니다.

 1. 모든 지표 패널은 [날짜 x 종목] 형태로 한 번에 계산하되,
    t 시점 판정에는 close_w.iloc[:t+1] 만 넘깁니다. 인덱스 슬라이싱을
    한 곳(_slice)으로 몰아 실수 여지를 없앴습니다.
 2. 진입은 돌파 '당일 종가'입니다. 종가는 그날 장 마감에 확정되므로
    실제로 체결 가능한 가격입니다. 다음날 시가로 바꾸려면
    entry_on_next_open을 켜세요.

정렬 기준
---------
미너비니는 손실을 짧게 자르는 것을 최우선으로 둡니다. 그래서 지표도
승률보다 기대값(expectancy)과 손익비를 먼저 봅니다.
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field, asdict

import numpy as np
import pandas as pd

from . import indicators as ind
from . import universe as uni
from . import vcp as vcp_mod

log = logging.getLogger(__name__)

EOK = 100_000_000


# ====================================================================
# 결과 컨테이너
# ====================================================================

@dataclass
class Trade:
    code: str
    name: str
    entry_date: str
    entry_price: float
    shares: int
    stop_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    hold_days: int = 0
    pnl: float = 0.0          # 수수료·세금 반영 손익 (원)
    pnl_pct: float = 0.0      # 진입가 대비 %
    r_multiple: float = 0.0   # 최초 리스크(진입-손절) 대비 배수
    mae_pct: float = 0.0      # 보유 중 최대 역행폭
    mfe_pct: float = 0.0      # 보유 중 최대 순행폭
    regime: str = ""          # 진입 시점 시장 국면
    rs_rating: int = 0
    vcp_score: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity: pd.Series = field(default_factory=pd.Series)
    metrics: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    signals_per_day: pd.Series = field(default_factory=pd.Series)

    def trades_df(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.to_dict() for t in self.trades])


# ====================================================================
# 엔진
# ====================================================================

class Backtester:
    """
    무거운 계산(이동평균, RS, Trend Template)은 생성 시 한 번만 합니다.
    파라미터 그리드 서치는 VCP 계층만 다시 돌리므로 반복이 저렴합니다.
    """

    def __init__(self, store, cfg, start: str | None = None, end: str | None = None):
        self.cfg = cfg
        self._prepare(store, start, end)

    # ---------- 준비 ----------

    def _prepare(self, store, start, end) -> None:
        cfg = self.cfg

        listing = store.listing_df()
        prices = store.price_panel()
        if prices.empty:
            raise RuntimeError("가격 데이터가 없습니다. 먼저 --backfill을 실행하세요.")

        prices["date"] = pd.to_datetime(prices["date"])

        # 유니버스는 '기간 전체' 기준으로 한 번 거릅니다.
        # 엄밀히는 매 시점의 시가총액으로 판정해야 하지만, 과거 시총 데이터가
        # 없으므로 종목명·코드 기반 필터(우선주/스팩/ETF)만 적용하고
        # 유동성은 시점별 거래대금으로 매일 판정합니다.
        clean = self._static_universe(listing, cfg)
        codes = [c for c in clean["code"].tolist() if c in set(prices["code"])]
        if not codes:
            raise RuntimeError("유니버스가 비었습니다.")

        sub = prices[prices["code"].isin(codes)]
        self.close = ind.to_wide(sub, "close")
        self.high = ind.to_wide(sub, "high")
        self.low = ind.to_wide(sub, "low")
        self.open = ind.to_wide(sub, "open")
        self.volume = ind.to_wide(sub, "volume")
        self.value = ind.to_wide(sub, "value")
        self.dates = self.close.index
        self.codes = list(self.close.columns)
        self.names = clean.set_index("code")["name"].to_dict()

        log.info("백테스트 대상 %d종목 / %d거래일 (%s ~ %s)",
                 len(self.codes), len(self.dates),
                 self.dates[0].date(), self.dates[-1].date())

        self._build_panels()
        self._set_window(start, end)

        # 반복 호출 대비 numpy 캐시
        self._np = {
            "close": self.close.to_numpy(dtype=float),
            "high": self.high.to_numpy(dtype=float),
            "low": self.low.to_numpy(dtype=float),
            "open": self.open.to_numpy(dtype=float),
            "volume": self.volume.to_numpy(dtype=float),
        }
        self._col_ix = {c: i for i, c in enumerate(self.codes)}

    @staticmethod
    def _static_universe(listing: pd.DataFrame, cfg) -> pd.DataFrame:
        df = listing.copy()
        if cfg.get("universe.exclude_preferred", True):
            df = df[~df.apply(lambda r: uni.is_preferred(str(r["code"]), str(r["name"])), axis=1)]
        bl = cfg.get("universe.name_blacklist", []) or []
        if bl:
            df = df[~df["name"].astype(str).str.contains("|".join(bl), na=False)]
        if cfg.get("universe.exclude_spac", True):
            df = df[~df["name"].astype(str).str.contains("스팩", na=False)]
        if cfg.get("universe.exclude_etf_etn_reit", True):
            df = df[~df["name"].astype(str).str.contains(
                r"ETF|ETN|리츠|인버스|레버리지|KODEX|TIGER|KBSTAR|ARIRANG|HANARO",
                na=False, case=False, regex=True)]
        return df.reset_index(drop=True)

    def _build_panels(self) -> None:
        """
        무거운 계산을 한 번만 합니다.

        이동평균, 52주 고저, RS 백분위는 임계값과 무관하게 결정되므로 캐시합니다.
        임계값에 의존하는 8조건 조립만 _tt_for()에서 매번 다시 합니다.
        이 분리가 없으면 그리드 서치에서 min_rs_rating 같은 파라미터가
        결과에 반영되지 않습니다.
        """
        cfg = self.cfg
        n_s = cfg.get("trend_template.ma_short", 50)
        n_m = cfg.get("trend_template.ma_mid", 150)
        n_l = cfg.get("trend_template.ma_long", 200)

        c = self.close
        self.ma_s = c.rolling(n_s, min_periods=n_s).mean()
        self.ma_m = c.rolling(n_m, min_periods=n_m).mean()
        self.ma_l = c.rolling(n_l, min_periods=n_l).mean()
        self.hi52 = self.high.rolling(252, min_periods=200).max()
        self.lo52 = self.low.rolling(252, min_periods=200).min()

        # 200일선 상승 추세 판정에 쓰는 두 재료
        self._ma_l_diff_up = (self.ma_l.diff() > 0)

        # RS Rating: 가중 수익률 → 매일 전 종목 백분위
        periods = cfg.get("rs.periods", [63, 126, 189, 252])
        weights = cfg.get("rs.weights", [0.4, 0.2, 0.2, 0.2])
        raw = None
        valid = pd.DataFrame(True, index=c.index, columns=c.columns)
        for p, w in zip(periods, weights):
            past = c.shift(p)
            valid &= past.notna() & (past > 0)
            term = (c / past) * w
            raw = term if raw is None else raw + term
        raw = raw.where(valid)
        self.rs = raw.rank(axis=1, pct=True, method="average") * 98 + 1

        # 유동성: 20일 평균 거래대금
        self.liq = self.value.rolling(20, min_periods=10).mean()

        # 시장 폭: 200일선 위 종목 비율
        above = (c > self.ma_l).where(self.ma_l.notna())
        self.breadth = above.mean(axis=1, skipna=True)

        self._tt_cache: dict[tuple, pd.DataFrame] = {}
        self.tt = self._tt_for(cfg)
        log.info("지표 패널 준비 완료 (1단계 통과 일평균 %.0f종목)",
                 self.tt.sum(axis=1).tail(250).mean())

    def _tt_for(self, cfg) -> pd.DataFrame:
        """주어진 임계값으로 8조건을 조립합니다. 조합별로 캐시합니다."""
        key = (
            cfg.get("trend_template.ma_long_rising_days", 22),
            cfg.get("trend_template.ma_long_rising_ratio", 0.6),
            cfg.get("trend_template.min_above_52w_low", 0.30),
            cfg.get("trend_template.max_below_52w_high", 0.25),
            cfg.get("trend_template.min_rs_rating", 70),
            cfg.get("trend_template.required_conditions", 8),
        )
        if key in self._tt_cache:
            return self._tt_cache[key]

        d, ratio, min_low_k, max_high_k, min_rs, required = key
        c = self.close

        direction = self.ma_l > self.ma_l.shift(d)
        up_ratio = self._ma_l_diff_up.rolling(d, min_periods=d).sum() / d
        c3 = direction & (up_ratio >= ratio)

        flags = [
            (c > self.ma_m) & (c > self.ma_l),
            self.ma_m > self.ma_l,
            c3,
            (self.ma_s > self.ma_m) & (self.ma_m > self.ma_l),
            c > self.ma_s,
            c >= self.lo52 * (1.0 + min_low_k),
            c >= self.hi52 * (1.0 - max_high_k),
            self.rs >= min_rs,
        ]
        passed = sum(f.fillna(False).astype(int) for f in flags)
        tt = (passed >= required) & self.ma_l.notna()

        self._tt_cache[key] = tt
        return tt

    def _set_window(self, start, end) -> None:
        """워밍업 구간을 건너뛴 실제 테스트 기간을 정합니다."""
        warmup = max(
            self.cfg.get("trend_template.ma_long", 200),
            max(self.cfg.get("rs.periods", [252])),
        ) + self.cfg.get("vcp.lookback_days", 120)

        i0 = min(warmup, len(self.dates) - 1)
        if start:
            s = pd.Timestamp(start)
            i0 = max(i0, int(self.dates.searchsorted(s)))
        i1 = len(self.dates) - 1
        if end:
            e = pd.Timestamp(end)
            i1 = min(i1, int(self.dates.searchsorted(e, side="right")) - 1)

        if i1 <= i0:
            raise RuntimeError(
                f"테스트 가능 기간이 없습니다. 데이터 {len(self.dates)}일, "
                f"필요 워밍업 {warmup}일. --backfill로 기간을 늘리세요."
            )
        self.i0, self.i1 = i0, i1
        log.info("테스트 구간 %s ~ %s (%d거래일)",
                 self.dates[i0].date(), self.dates[i1].date(), i1 - i0 + 1)

    # ---------- 실행 ----------

    def run(self, overrides: dict | None = None, progress: bool = True) -> BacktestResult:
        cfg = _OverrideCfg(self.cfg, overrides or {})

        cash = float(cfg.get("backtest.initial_capital", 100_000_000))
        risk_pct = cfg.get("backtest.risk_per_trade_pct", 0.5) / 100
        max_pos_pct = cfg.get("backtest.max_position_pct", 25) / 100
        max_open = cfg.get("backtest.max_open_positions", 8)
        max_risk_pct = cfg.get("backtest.max_stop_distance_pct", 12)
        fee = cfg.get("backtest.fee_pct", 0.015) / 100
        tax = cfg.get("backtest.sell_tax_pct", 0.18) / 100
        slip = cfg.get("backtest.slippage_pct", 0.15) / 100
        min_liq = cfg.get("universe.min_avg_value_eok", 10) * EOK
        respect_regime = cfg.get("backtest.respect_regime", True)
        breadth_floor = cfg.get("regime.breadth_risk_off", 0.25)
        next_open = cfg.get("backtest.entry_on_next_open", False)

        lookback = cfg.get("vcp.lookback_days", 120)
        npd = self._np
        tt_panel = self._tt_for(cfg)   # 오버라이드가 1단계까지 반영되어야 합니다

        trades: list[Trade] = []
        open_pos: dict[str, dict] = {}
        equity_curve: list[float] = []
        signal_counts: list[int] = []
        pending: list[dict] = []   # 다음날 시가 진입용

        for t in range(self.i0, self.i1 + 1):
            date = self.dates[t]

            # ---- 1. 보유 포지션 청산 판정 (진입보다 먼저) ----
            for code in list(open_pos.keys()):
                pos = open_pos[code]
                j = self._col_ix[code]
                lo, hi = npd["low"][t, j], npd["high"][t, j]
                op, cl = npd["open"][t, j], npd["close"][t, j]
                if not np.isfinite(cl):
                    continue

                pos["hold"] += 1
                if np.isfinite(hi):
                    pos["mfe"] = max(pos["mfe"], (hi / pos["entry"] - 1) * 100)
                if np.isfinite(lo):
                    pos["mae"] = min(pos["mae"], (lo / pos["entry"] - 1) * 100)

                exit_price, reason = self._check_exit(pos, t, j, cfg, npd)
                if exit_price is None:
                    continue

                gross = exit_price * pos["shares"]
                cost = gross * (fee + tax)
                cash += gross - cost
                entry_cost = pos["entry"] * pos["shares"]
                pnl = (gross - cost) - entry_cost * (1 + fee)
                risk_unit = (pos["entry"] - pos["init_stop"]) * pos["shares"]

                trades.append(Trade(
                    code=code, name=self.names.get(code, code),
                    entry_date=pos["entry_date"], entry_price=round(pos["entry"], 1),
                    shares=pos["shares"], stop_price=round(pos["init_stop"], 1),
                    exit_date=date.strftime("%Y-%m-%d"), exit_price=round(exit_price, 1),
                    exit_reason=reason, hold_days=pos["hold"],
                    pnl=round(pnl), pnl_pct=round((exit_price / pos["entry"] - 1) * 100, 2),
                    r_multiple=round(pnl / risk_unit, 2) if risk_unit > 0 else 0.0,
                    mae_pct=round(pos["mae"], 2), mfe_pct=round(pos["mfe"], 2),
                    regime=pos["regime"], rs_rating=pos["rs"], vcp_score=pos["vcp"],
                ))
                del open_pos[code]

            # ---- 2. 대기 주문 체결 (다음날 시가 방식) ----
            if next_open and pending:
                for order in pending:
                    code = order["code"]
                    if code in open_pos or len(open_pos) >= max_open:
                        continue
                    j = self._col_ix[code]
                    px = npd["open"][t, j]
                    if not np.isfinite(px) or px <= 0:
                        continue
                    entry = px * (1 + slip)
                    cash = self._open_position(
                        open_pos, order, entry, cash, date, cfg,
                        risk_pct, max_pos_pct, fee)
                pending = []

            # ---- 3. 신규 신호 탐색 ----
            equity = cash + sum(
                npd["close"][t, self._col_ix[c]] * p["shares"]
                for c, p in open_pos.items()
                if np.isfinite(npd["close"][t, self._col_ix[c]])
            )
            equity_curve.append(equity)

            regime = self._regime_at(t, breadth_floor)
            if respect_regime and regime == "risk_off":
                signal_counts.append(0)
                continue
            if len(open_pos) >= max_open:
                signal_counts.append(0)
                continue

            candidates = tt_panel.iloc[t]
            passing = [c for c in self.codes if bool(candidates.get(c, False))]
            found = 0

            for code in passing:
                if code in open_pos:
                    continue
                j = self._col_ix[code]

                liq = self.liq.iat[t, j]
                if not np.isfinite(liq) or liq < min_liq:
                    continue

                lo_i = max(0, t - lookback + 1)
                res = vcp_mod.detect_arrays(
                    npd["high"][lo_i:t + 1, j],
                    npd["low"][lo_i:t + 1, j],
                    npd["close"][lo_i:t + 1, j],
                    npd["volume"][lo_i:t + 1, j],
                    cfg,
                )
                if not (res.detected and res.breakout):
                    continue
                if not np.isfinite(res.risk_pct) or res.risk_pct > max_risk_pct:
                    continue

                found += 1
                order = {
                    "code": code, "stop": res.stop_price,
                    "rs": int(self.rs.iat[t, j]) if np.isfinite(self.rs.iat[t, j]) else 0,
                    "vcp": res.score, "regime": regime,
                }

                if next_open:
                    pending.append(order)
                else:
                    entry = npd["close"][t, j] * (1 + slip)
                    cash = self._open_position(
                        open_pos, order, entry, cash, date, cfg,
                        risk_pct, max_pos_pct, fee)
                    if len(open_pos) >= max_open:
                        break

            signal_counts.append(found)

            if progress and (t - self.i0) % 60 == 0:
                log.info("  %s  자산 %.0f만원  보유 %d  누적거래 %d",
                         date.date(), equity / 10000, len(open_pos), len(trades))

        # 잔여 포지션 청산
        t = self.i1
        for code, pos in list(open_pos.items()):
            j = self._col_ix[code]
            px = npd["close"][t, j]
            if not np.isfinite(px):
                continue
            gross = px * pos["shares"]
            cash += gross - gross * (fee + tax)

        idx = self.dates[self.i0:self.i1 + 1]
        result = BacktestResult(
            trades=trades,
            equity=pd.Series(equity_curve, index=idx[:len(equity_curve)]),
            signals_per_day=pd.Series(signal_counts, index=idx[:len(signal_counts)]),
            params=dict(overrides or {}),
        )
        result.metrics = compute_metrics(result, cfg)
        return result

    # ---------- 보조 ----------

    def _open_position(self, open_pos, order, entry, cash, date, cfg,
                       risk_pct, max_pos_pct, fee) -> float:
        """포지션을 열고 남은 현금을 돌려줍니다."""
        stop = order["stop"]
        if entry <= stop:
            return cash
        equity_now = cash + sum(p["entry"] * p["shares"] for p in open_pos.values())
        risk_amount = equity_now * risk_pct
        per_share_risk = entry - stop
        shares = int(risk_amount / per_share_risk)

        cap = int(equity_now * max_pos_pct / entry)
        shares = min(shares, cap)
        affordable = int(cash / (entry * (1 + fee)))
        shares = min(shares, affordable)
        if shares <= 0:
            return cash

        cost = entry * shares * (1 + fee)
        open_pos[order["code"]] = {
            "entry": entry, "shares": shares,
            "init_stop": stop, "stop": stop,
            "entry_date": date.strftime("%Y-%m-%d"), "hold": 0,
            "mae": 0.0, "mfe": 0.0, "be_moved": False,
            "rs": order["rs"], "vcp": order["vcp"], "regime": order["regime"],
        }
        return cash - cost

    def _check_exit(self, pos, t, j, cfg, npd):
        """청산 여부를 판정합니다. (가격, 사유) 또는 (None, '')."""
        lo = npd["low"][t, j]
        op = npd["open"][t, j]
        cl = npd["close"][t, j]

        # 1) 손절. 갭하락이면 시가로 체결됩니다.
        #    초기 손절선인지 끌어올린 손절선인지 구분해야 통계가 의미를 가집니다.
        #    둘을 뭉뚱그리면 "손절 평균 +29%" 같은 모순된 표가 나옵니다.
        if np.isfinite(lo) and lo <= pos["stop"]:
            px = min(pos["stop"], op) if np.isfinite(op) else pos["stop"]
            if pos["stop"] <= pos["init_stop"] * 1.0001:
                reason = "손절"
            elif px >= pos["entry"]:
                reason = "추적익절"
            else:
                reason = "추적손절"
            return px, reason

        gain = (cl / pos["entry"] - 1) * 100

        # 2) 본전 스톱: 일정 수익 도달 후 손절선을 진입가로 올립니다.
        be_at = cfg.get("backtest.breakeven_at_pct", 8)
        if not pos["be_moved"] and gain >= be_at:
            pos["stop"] = max(pos["stop"], pos["entry"])
            pos["be_moved"] = True

        # 3) 추적 손절: 50일선 이탈
        if cfg.get("backtest.trail_with_ma50", True) and pos["be_moved"]:
            ma = self.ma_s.iat[t, j]
            if np.isfinite(ma):
                pos["stop"] = max(pos["stop"], ma * 0.98)

        # 4) 시간 손절: 일정 기간 안에 못 오르면 자리를 비웁니다.
        time_stop = cfg.get("backtest.time_stop_days", 25)
        time_min_gain = cfg.get("backtest.time_stop_min_gain_pct", 3)
        if pos["hold"] >= time_stop and gain < time_min_gain:
            return cl, "시간손절"

        # 5) 최대 보유일
        max_hold = cfg.get("backtest.max_hold_days", 120)
        if pos["hold"] >= max_hold:
            return cl, "기간만료"

        # 6) 목표 수익 (0이면 미사용)
        target = cfg.get("backtest.target_pct", 0)
        if target and gain >= target:
            return cl, "목표도달"

        return None, ""

    def _regime_at(self, t: int, floor: float) -> str:
        b = self.breadth.iat[t]
        if not np.isfinite(b):
            return "neutral"
        if b < floor:
            return "risk_off"
        if b < self.cfg.get("regime.breadth_caution", 0.40):
            return "caution"
        return "risk_on"


class _OverrideCfg:
    """원본 설정을 건드리지 않고 일부 값만 덮어씁니다. 그리드 서치용."""

    def __init__(self, base, overrides: dict):
        self._base = base
        self._ov = overrides

    def get(self, path: str, default=None):
        if path in self._ov:
            return self._ov[path]
        return self._base.get(path, default)


# ====================================================================
# 성과 지표
# ====================================================================

def compute_metrics(result: BacktestResult, cfg) -> dict:
    df = result.trades_df()
    eq = result.equity

    if df.empty:
        return {
            "trades": 0, "note": "거래 없음",
            "final_equity": float(eq.iloc[-1]) if len(eq) else 0.0,
        }

    wins = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    n = len(df)
    win_rate = len(wins) / n * 100

    avg_win = wins["pnl_pct"].mean() if len(wins) else 0.0
    avg_loss = losses["pnl_pct"].mean() if len(losses) else 0.0
    payoff = abs(avg_win / avg_loss) if avg_loss else float("inf")

    # 기대값: 미너비니가 승률보다 먼저 보는 숫자
    expectancy = (win_rate / 100) * avg_win + (1 - win_rate / 100) * avg_loss
    expectancy_r = df["r_multiple"].mean()

    gross_win = wins["pnl"].sum()
    gross_loss = abs(losses["pnl"].sum())
    profit_factor = gross_win / gross_loss if gross_loss else float("inf")

    start_eq = float(eq.iloc[0]) if len(eq) else 0.0
    end_eq = float(eq.iloc[-1]) if len(eq) else 0.0
    total_ret = (end_eq / start_eq - 1) * 100 if start_eq else 0.0

    years = len(eq) / 246 if len(eq) else 0
    cagr = ((end_eq / start_eq) ** (1 / years) - 1) * 100 if years > 0.1 and start_eq else 0.0

    peak = eq.cummax()
    dd = (eq / peak - 1) * 100
    max_dd = float(dd.min()) if len(dd) else 0.0

    daily = eq.pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * np.sqrt(246)) if len(daily) > 5 and daily.std() else 0.0

    by_regime = {}
    for reg, g in df.groupby("regime"):
        by_regime[reg] = {
            "trades": len(g),
            "win_rate": round(len(g[g["pnl"] > 0]) / len(g) * 100, 1),
            "avg_r": round(g["r_multiple"].mean(), 2),
            "total_pnl": round(g["pnl"].sum()),
        }

    by_reason = {
        str(k): {"count": len(g), "avg_pct": round(g["pnl_pct"].mean(), 2)}
        for k, g in df.groupby("exit_reason")
    }

    return {
        "trades": n,
        "win_rate": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "payoff_ratio": round(payoff, 2) if np.isfinite(payoff) else None,
        "expectancy_pct": round(expectancy, 2),
        "expectancy_r": round(expectancy_r, 3),
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else None,
        "total_return_pct": round(total_ret, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(float(sharpe), 2),
        "avg_hold_days": round(df["hold_days"].mean(), 1),
        "final_equity": round(end_eq),
        "by_regime": by_regime,
        "by_exit_reason": by_reason,
    }


# ====================================================================
# 파라미터 그리드 서치
# ====================================================================

def grid_search(bt: Backtester, grid: dict[str, list], top: int = 15) -> pd.DataFrame:
    """
    파라미터 조합별 성과를 비교합니다.

    정렬 기준은 총수익이 아니라 기대값(R 배수)입니다. 총수익으로 정렬하면
    거래 몇 건이 우연히 크게 터진 조합이 1등을 차지합니다.
    """
    keys = list(grid.keys())
    combos = list(itertools.product(*[grid[k] for k in keys]))
    log.info("그리드 서치 %d조합", len(combos))

    rows = []
    for i, combo in enumerate(combos, 1):
        ov = dict(zip(keys, combo))
        res = bt.run(ov, progress=False)
        m = res.metrics
        row = dict(ov)
        row.update({
            "trades": m.get("trades", 0),
            "win_rate": m.get("win_rate", 0),
            "expectancy_r": m.get("expectancy_r", 0),
            "profit_factor": m.get("profit_factor"),
            "total_return_pct": m.get("total_return_pct", 0),
            "max_dd_pct": m.get("max_drawdown_pct", 0),
        })
        rows.append(row)
        log.info("  [%d/%d] %s → 거래 %d, 기대값 %.3fR, 수익 %.1f%%",
                 i, len(combos), ov, row["trades"], row["expectancy_r"],
                 row["total_return_pct"])

    df = pd.DataFrame(rows)
    # 표본이 너무 적은 조합은 신뢰할 수 없습니다.
    enough = df[df["trades"] >= 10]
    if enough.empty:
        log.warning("거래 10건 이상인 조합이 없습니다. 기간을 늘리거나 조건을 완화하세요.")
        enough = df
    result = enough.sort_values("expectancy_r", ascending=False).head(top).reset_index(drop=True)

    # 화면 표시용 짧은 컬럼명 (원본 dotted 키는 CSV에 그대로 남깁니다)
    return result


# ====================================================================
# 보고서
# ====================================================================

def report_text(result: BacktestResult) -> str:
    m = result.metrics
    if m.get("trades", 0) == 0:
        return ("백테스트 결과: 거래 0건\n"
                "  조건이 너무 빡빡하거나 테스트 기간이 짧습니다.\n"
                "  config.yaml에서 min_rs_rating이나 vcp 조건을 완화해보세요.")

    lines = [
        "=" * 60,
        " 백테스트 결과",
        "=" * 60,
        f"  거래 횟수      {m['trades']}건 (평균 보유 {m['avg_hold_days']}일)",
        f"  승률           {m['win_rate']}%",
        f"  평균 수익      {m['avg_win_pct']:+.2f}%",
        f"  평균 손실      {m['avg_loss_pct']:+.2f}%",
        f"  손익비         {m['payoff_ratio']}",
        "",
        f"  기대값         {m['expectancy_pct']:+.2f}%  ({m['expectancy_r']:+.3f}R)",
        f"  손익비율(PF)   {m['profit_factor']}",
        "",
        f"  총수익         {m['total_return_pct']:+.2f}%",
        f"  연환산(CAGR)   {m['cagr_pct']:+.2f}%",
        f"  최대낙폭(MDD)  {m['max_drawdown_pct']:.2f}%",
        f"  샤프           {m['sharpe']}",
        f"  최종 자산      {m['final_equity']:,}원",
    ]

    if m.get("by_regime"):
        lines += ["", "-" * 60, " 시장 국면별", "-" * 60]
        label = {"risk_on": "정상", "caution": "선별", "risk_off": "관망", "neutral": "중립"}
        for reg, s in sorted(m["by_regime"].items()):
            lines.append(f"  {label.get(reg, reg):4s}  {s['trades']:3d}건  "
                         f"승률 {s['win_rate']:5.1f}%  평균 {s['avg_r']:+.2f}R  "
                         f"손익 {s['total_pnl']:>+12,}원")

    if m.get("by_exit_reason"):
        lines += ["", "-" * 60, " 청산 사유별", "-" * 60]
        for reason, s in sorted(m["by_exit_reason"].items(),
                                key=lambda x: -x[1]["count"]):
            lines.append(f"  {reason:8s}  {s['count']:3d}건  평균 {s['avg_pct']:+6.2f}%")

    lines += ["", "=" * 60]

    exp = m["expectancy_r"]
    if exp > 0.25:
        verdict = "기대값이 양호합니다. 실매매 검토 가능한 수준입니다."
    elif exp > 0:
        verdict = "기대값은 양수지만 얇습니다. 수수료와 슬리피지에 취약합니다."
    else:
        verdict = "기대값이 음수입니다. 이 설정으로는 매매하면 안 됩니다."
    lines.append(f" 판정: {verdict}")
    lines.append("=" * 60)

    return "\n".join(lines)
