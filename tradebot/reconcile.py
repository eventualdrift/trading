"""Paper vs backtest, trade by trade (reporting only).

Replays the backtest over the paper period on frozen candles and compares:
* satellite signals (coin, strategy, candle): which each side saw, and why the other didn't;
* for signals both acted on: entry and exit against the modelled fills, exit reason and candle,
  fees, R;
* taken vs skipped: the backtest's slot decisions (seeded with the paper positions already open
  when the window starts) against what paper did;
* the core: daily target weights and the rebalances they implied, fills and fees.

What the bot was working with at each moment (coin list, active strategies, core targets, scan
errors) comes from its activity log (db ``botlog``). Before that log existed the report says
which parts are approximated.
"""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from .backtest.engine import Trade, backtest_populated, reward_risk
from .config import BotConfig
from .core import trend_weights_series
from .db import Database
from .strategies import make_strategy
from .timeframes import index_ms, last_closed_open_ms, tf_ms

UP_GAP_MS = 20 * 60_000  # snapshots every 15 min: none within 20 min of a moment = the bot was not running
SCAN_LAG_MS = 5 * 60_000  # a candle's scan runs within minutes of its close: what was in force then counts
DAY_MS = 86_400_000
# the backtest replays each coin/strategy one trade at a time, before slots are allotted: a trade it then
# skips for lack of a slot still blocks that coin/strategy's next signals, which live is free to take
SKIPPED_SHADOW = "no trade: its earlier trade here, skipped for lack of a slot, was still running"


# --------------------------------------------------------------------- what paper had
class Timeline:
    """A value that changes over time, from the activity log; ``fallback`` before the first entry."""

    def __init__(self, entries: list[tuple[int, object]], fallback):
        self.entries, self.fallback = entries, fallback
        self.first = entries[0][0] if entries else None

    def at(self, ms: int):
        value = self.fallback
        for ts, v in self.entries:
            if ts <= ms:
                value = v
            else:
                break
        return value

    def approximate(self, ms: int) -> bool:
        return self.first is None or ms < self.first


def combo_key(strategy: str, timeframe: str, params: dict | None) -> str:
    return f"{strategy}@{timeframe}|{json.dumps(params or {}, sort_keys=True)}"


def _combos(data) -> dict[str, tuple[str, str, dict]]:
    return {combo_key(c["strategy"], c["timeframe"], c.get("params")): (c["strategy"], c["timeframe"], c.get("params") or {})
            for c in (data or {}).get("combos", [])}


def uptime(db: Database, mode: str):
    """Was the bot running at a moment? Judged from its equity snapshots (every 15 minutes)."""
    snaps = db.snapshots(mode)
    ts = np.array([int(t.value // 1_000_000) for t in snaps.index], dtype=np.int64) if len(snaps) else np.array([], np.int64)
    usual = int(np.median(np.diff(ts))) if len(ts) > 2 else 0
    near_ms = max(UP_GAP_MS, int(usual * 1.4))

    def up(ms: int) -> bool:
        if not len(ts):
            return False
        i = np.searchsorted(ts, ms)
        return any(abs(int(ts[j]) - ms) <= near_ms for j in (i - 1, i) if 0 <= j < len(ts))

    downs = [(int(a), int(b)) for a, b in zip(ts[:-1], ts[1:]) if b - a > 2 * near_ms]
    return up, downs, (int(ts[0]) if len(ts) else None), (int(ts[-1]) if len(ts) else None)


# --------------------------------------------------------------------- what the backtest does
@dataclass
class BtSignal:
    symbol: str
    timeframe: str
    strategy: str
    combo: str
    side: str
    candle_ms: int
    close: float
    stop: float
    target: float
    rr: float
    trade: Trade | None = None  # the backtest's trade for this signal
    no_trade: str = ""  # why there is none
    blocked_by: Trade | None = None  # the backtest's earlier trade on this coin/strategy that was still running


def backtest_signals(datasets: dict[str, dict[str, pd.DataFrame]], combos: dict[str, tuple[str, str, dict]],
                     cfg: BotConfig, context, since_ms: int, end_ms: int) -> list[BtSignal]:
    costs = cfg.costs_model()
    out = []
    for key, (name, tf, params) in combos.items():
        strat = make_strategy(name, params)
        for sym, df in datasets.get(tf, {}).items():
            if len(df) < strat.warmup + 2:
                continue
            pop = strat.populate(df, context, tf)
            trades = backtest_populated(pop, strat, costs, symbol=sym, timeframe=tf, allow_short=cfg.allow_short,
                                        breakeven_at_r=cfg.risk.breakeven_at_r, min_reward_risk=cfg.risk.min_reward_risk)
            by_idx = {t.signal_idx: t for t in trades}
            busy = {}  # candle -> the trade this coin/strategy was already in
            for t in trades:
                busy.update(dict.fromkeys(range(t.signal_idx + 1, int(pop.index.get_loc(t.exit_time))), t))
            opens = index_ms(pop.index)
            closes = pop["close"].to_numpy(dtype=float)
            for side in ("long", "short"):
                if side == "short" and not cfg.allow_short:
                    continue
                flags = pop[f"enter_{side}"].to_numpy()
                sls, tps = pop[f"{side}_sl"].to_numpy(dtype=float), pop[f"{side}_tp"].to_numpy(dtype=float)
                for i in np.flatnonzero(flags):
                    if i < strat.warmup:
                        continue
                    close_ms = int(opens[i]) + tf_ms(tf)
                    if not since_ms <= close_ms <= end_ms:
                        continue
                    rr = reward_risk(side, closes[i], sls[i], tps[i])
                    if rr < cfg.risk.min_reward_risk:
                        continue
                    trade = by_idx.get(int(i))
                    why = ("" if trade is not None else
                           "no trade: that coin/strategy was already in a trade" if i in busy else
                           "no trade yet: next candle not in the data" if i >= len(pop) - 1 else
                           "no trade: limit not filled" if costs.limit_entry else
                           "no trade: next open already beyond stop or target")
                    out.append(BtSignal(sym, tf, name, key, side, int(opens[i]), float(closes[i]), float(sls[i]),
                                        float(tps[i]), rr, trade, why, busy.get(int(i))))
    return out


def _slot_decisions(cfg: BotConfig, candidates: list[Trade], seeds: list[Trade], since_ms: int, end_ms: int,
                    exits_at_close: bool = False) -> dict:
    """The backtest's slot decisions (live rules and order) -> {id(trade): 'taken' | reason}.

    ``exits_at_close``: a held trade frees its slot at the close of its exit candle (when paper has
    sold) instead of at that candle's open, where the backtest frees it."""
    from dataclasses import replace

    from .portfolio import simulate_satellite

    idx = pd.date_range(pd.Timestamp(min(since_ms, *(int(s.entry_time.value // 1_000_000) for s in seeds))
                                     if seeds else since_ms, unit="ms", tz="UTC").floor("D"),
                        pd.Timestamp(end_ms, unit="ms", tz="UTC").ceil("D"), freq="D")
    run_on = {id(t): t for t in candidates}
    if exits_at_close:
        run_on = {id(t): replace(t, exit_time=t.exit_time + pd.Timedelta(milliseconds=tf_ms(t.timeframe)))
                  for t in candidates}
    back = {id(v): k for k, v in run_on.items()}
    run = simulate_satellite(seeds + list(run_on.values()), cfg, idx)
    out = {back[id(t)]: "taken" for t in run.taken if id(t) in back}
    for reason, ts in run.skipped.items():
        for t in ts:
            if id(t) in back:
                out[back[id(t)]] = reason
    return out


# --------------------------------------------------------------------- the comparison
@dataclass
class Row:
    time: str
    symbol: str
    setup: str
    side: str
    candle_ms: int
    paper: str  # opened / skipped: why / filtered / (no signal: why)
    backtest: str  # taken / reason / (no signal)
    backtest_exits_at_close: str = ""  # the same, with slots freed at the exit candle's close
    entry_paper: float | None = None
    entry_model: float | None = None
    entry_bps: float | None = None
    exit_paper: str | None = None
    exit_model: str | None = None
    exit_same_candle: bool | None = None
    exit_bps: float | None = None
    r_paper: float | None = None
    r_model: float | None = None
    fees_paper_pct: float | None = None
    fees_model_pct: float | None = None
    note: str = ""


@dataclass
class Reconciliation:
    since_ms: int
    end_ms: int
    rows: list[Row] = field(default_factory=list)
    uptime_share: float = 0.0
    downs: list[tuple[int, int]] = field(default_factory=list)
    approx_universe_until: int | None = None
    approx_selection_until: int | None = None
    ml_active: bool = False
    core_days: list[dict] = field(default_factory=list)
    core_trades: list[dict] = field(default_factory=list)
    core_expected_missing: list[dict] = field(default_factory=list)
    core_late: list[dict] = field(default_factory=list)
    core_logged_from: int | None = None  # first logged day's close
    core_rule_days: int = 0
    core_rule_diffs: list[dict] = field(default_factory=list)
    core_days_missing: list[int] = field(default_factory=list)  # days after logging began with no core check
    header: list[str] = field(default_factory=list)


def _bps(a: float | None, b: float | None, sign: float = 1.0) -> float | None:
    if a is None or b is None or not b:
        return None
    return sign * (a - b) / b * 1e4


def reconcile(db: Database, cfg: BotConfig, datasets, context, daily_closes: dict[str, pd.Series],
              since_ms: int, end_ms: int, current_selection, fallback_universe: list[str]) -> Reconciliation:
    mode = cfg.mode
    rec = Reconciliation(since_ms, end_ms)
    up, downs, first_snap, last_snap = uptime(db, mode)
    rec.downs = [(a, b) for a, b in downs if b >= since_ms and a <= end_ms]

    # --- what paper was working with
    sel_fallback = {"combos": [{"strategy": c.strategy, "timeframe": c.timeframe, "params": c.params}
                               for c in (current_selection.selected if current_selection else [])]}
    selections = Timeline(db.botlog(mode, "selection"), sel_fallback)
    universes = Timeline(db.botlog(mode, "universe"), fallback_universe)
    rec.approx_selection_until = selections.first
    rec.approx_universe_until = universes.first
    rec.ml_active = any((d or {}).get("ml") for _, d in selections.entries)
    scan_errors = {}
    for ts, d in db.botlog(mode, "scan_errors", since_ms - 86_400_000):
        for e in d.get("errors", []):
            scan_errors[(e.split(" ")[0], d["tf"], int(d["candle"]))] = e

    # --- backtest signals, kept where paper had that combo active and that coin in its list
    all_combos = {}
    for data in [sel_fallback] + [d for _, d in selections.entries]:
        all_combos.update(_combos(data))
    bt = backtest_signals(datasets, all_combos, cfg, context, since_ms, end_ms)

    def active(s: BtSignal) -> bool:
        t = s.candle_ms + tf_ms(s.timeframe) + SCAN_LAG_MS
        return s.combo in _combos(selections.at(t)) and s.symbol in set(universes.at(t) or [])

    def key_of(s: BtSignal) -> tuple:
        return s.symbol, s.timeframe, s.strategy, s.side, s.candle_ms

    outside = {key_of(s) for s in bt if not active(s)}  # coin not in paper's list / strategy not active then
    bt = [s for s in bt if active(s)]
    bt_by_key = {key_of(s): s for s in bt}

    # --- paper signals and positions
    paper = {}
    for s in db.signals_since(since_ms - 86_400_000):
        close_ms = s.candle_time + tf_ms(s.timeframe)
        if since_ms <= close_ms <= end_ms:
            paper[(s.symbol, s.timeframe, s.strategy, s.side, s.candle_time)] = s
    positions = db.positions_by_signal(mode)

    # --- the backtest's slot decisions, seeded with paper positions open when the window starts
    seeds = []
    for p in db.positions_with_status(mode, ("open", "closed", "working", "unknown")):
        if p.opened_at < since_ms and (p.closed_at is None or p.closed_at > since_ms):
            exit_ms = p.closed_at if p.closed_at else end_ms + 86_400_000
            seeds.append(Trade(symbol=p.symbol, timeframe=p.timeframe, strategy=p.strategy, side=p.side,
                               signal_idx=-1, signal_time=pd.Timestamp(p.opened_at, unit="ms", tz="UTC"),
                               entry_time=pd.Timestamp(p.opened_at, unit="ms", tz="UTC"),
                               exit_time=pd.Timestamp(exit_ms, unit="ms", tz="UTC"), entry_price=p.entry_price,
                               exit_price=p.exit_price or p.entry_price, stop_loss=p.initial_stop,
                               take_profit=p.take_profit, reason="paper position", bars_held=0, r_multiple=0.0,
                               return_pct=0.0, stop_pct=abs(p.entry_price - p.initial_stop) / p.entry_price))
    candidates = [s.trade for s in bt if s.trade is not None]
    # earlier trades (inside the window) that block signals, so their slot decisions are known too;
    # the ones from before the window are not replayed: paper's own open positions stand in for them
    def in_window(t: Trade) -> bool:
        return int(t.signal_time.value // 1_000_000) + tf_ms(t.timeframe) >= since_ms

    extra = {id(s.blocked_by): s.blocked_by for s in bt if s.blocked_by is not None and in_window(s.blocked_by)}
    candidates += [t for k, t in extra.items() if k not in {id(c) for c in candidates}]
    decisions = _slot_decisions(cfg, candidates, seeds, since_ms, end_ms) if candidates else {}
    at_close = _slot_decisions(cfg, candidates, seeds, since_ms, end_ms, exits_at_close=True) if candidates else {}

    fee, entry_fee = cfg.costs.fee_rate, cfg.costs_model().entry_fee
    for key in sorted(set(bt_by_key) | set(paper), key=lambda k: (k[4], k[0])):
        s, ps = bt_by_key.get(key), paper.get(key)
        sym, tf, strat, side, candle = key
        close_ms = candle + tf_ms(tf)
        row = Row(time=pd.Timestamp(close_ms, unit="ms", tz="UTC").strftime("%m-%d %H:%M"), symbol=sym,
                  setup=f"{strat}@{tf}", side=side, candle_ms=candle, paper="", backtest="")
        # paper side
        pos = positions.get(ps.id) if ps is not None else None
        if ps is None:
            why = ("bot not running" if not up(close_ms + 60_000) else
                   f"scan error: {scan_errors[(sym, tf, candle)]}" if (sym, tf, candle) in scan_errors else
                   "coin list approximated" if universes.approximate(close_ms) else "not explained")
            row.paper = f"(no signal: {why})"
        elif ps.status == "opened" and pos is not None:
            row.paper = {"open": "opened (still open)", "closed": "opened", "missed": "limit missed",
                         "working": "limit working", "failed": "entry failed"}.get(pos.status, pos.status)
        else:
            row.paper = ps.status + (f": {ps.note}" if ps.note else "")
        # backtest side
        if s is None:
            row.backtest = ("(no signal)" if key not in outside else
                            "(not replayed: coin outside paper's logged list, or strategy not active then)")
        elif s.trade is None:
            row.backtest = s.no_trade
            if s.blocked_by is not None and id(s.blocked_by) not in extra and id(s.blocked_by) not in decisions:
                row.backtest = "no trade: that coin/strategy was in a trade from before the window"
            elif s.blocked_by is not None and decisions.get(id(s.blocked_by), "taken") != "taken":
                row.backtest = SKIPPED_SHADOW
        else:
            row.backtest = decisions.get(id(s.trade), "taken")
        if s is not None:
            t0 = s.trade if s.trade is not None else None
            row.backtest_exits_at_close = (at_close.get(id(t0), "taken") if t0 is not None else
                                           SKIPPED_SHADOW if s.blocked_by is not None
                                           and at_close.get(id(s.blocked_by), "taken") != "taken" else s.no_trade)
        # fills, where both sides entered
        t = s.trade if s is not None else None
        if pos is not None and pos.status in ("open", "closed") and t is not None and row.backtest == "taken":
            sign = 1.0 if side == "long" else -1.0
            row.entry_paper, row.entry_model = pos.entry_price, t.entry_price
            row.entry_bps = _bps(pos.entry_price, t.entry_price, sign)  # + = paper paid more than modelled
            row.exit_model = f"{t.reason} @ {t.exit_price:.6g}"
            row.r_model = t.r_multiple
            notional = pos.entry_price * pos.amount
            row.fees_model_pct = 100 * (entry_fee + (fee * t.exit_price / t.entry_price if t.reason != "end_of_data" else 0))
            if pos.status == "closed":
                row.exit_paper = f"{pos.exit_reason} @ {pos.exit_price:.6g}"
                row.r_paper = pos.r_multiple
                bar_open = int(t.exit_time.value // 1_000_000)
                row.exit_same_candle = bar_open <= pos.closed_at <= bar_open + tf_ms(tf) + 5 * 60_000
                row.exit_bps = _bps(pos.exit_price, t.exit_price, -sign)  # + = paper sold for less
                row.fees_paper_pct = pos.fees / notional * 100 if notional else None
                if t.reason == "end_of_data":
                    row.note = "backtest trade still open at the data end"
            else:
                row.exit_paper = "still open"
                if t.reason != "end_of_data":
                    row.note = f"backtest exited ({t.reason}) but paper is still open"
        rec.rows.append(row)

    # --- uptime over the window
    step = 15 * 60_000
    checks = range(since_ms, end_ms, step)
    rec.uptime_share = sum(up(m) for m in checks) / max(len(checks), 1)

    # --- the core
    if daily_closes:
        _reconcile_core(rec, db, cfg, daily_closes, since_ms, end_ms)
    return rec


def rule_trades(cfg: BotConfig, targets: dict, closes: dict, cash: float, holdings: dict) -> dict[str, tuple[str, float]]:
    """The backtest's rebalance rule (core.simulate_core_detail) applied to a given state:
    {symbol: (side, value traded)} for the coins it would trade that day."""
    c, costs = cfg.core, cfg.costs
    eq = cash + sum(q * closes.get(s, 0.0) for s, q in holdings.items())
    slot = eq / max(len(c.symbols), 1)
    threshold = max(c.min_trade_usd, c.drift_tolerance * slot)
    plans = []
    for s in c.symbols:
        w, px = targets.get(s), closes.get(s)
        if w is None or w != w or not px:
            continue
        delta = slot * w - holdings.get(s, 0.0) * px
        if abs(delta) >= threshold:
            plans.append((delta, s))
    out = {}
    buy_cost = (1 + costs.fee_rate) * (1 + costs.slippage_rate)
    for delta, s in sorted(plans):  # sells first free the cash, as in the backtest
        px = closes[s]
        if delta < 0:
            q = min(-delta / px, holdings.get(s, 0.0))
            cash += px * (1 - costs.slippage_rate) * q * (1 - costs.fee_rate)
            if q > 0:
                out[s] = ("sell", q * px)
        else:
            spend = min(delta, cash / buy_cost)
            cash -= spend * buy_cost
            if spend > 0:
                out[s] = ("buy", spend)
    return out


def _reconcile_core(rec: Reconciliation, db: Database, cfg: BotConfig, daily_closes: dict[str, pd.Series],
                    since_ms: int, end_ms: int) -> None:
    mode, c = cfg.mode, cfg.core
    delay = int(cfg.candle_close_delay_seconds * 1000)
    closes_ms = {sym: pd.Series(s.to_numpy(dtype=float), index=index_ms(s.index)) for sym, s in daily_closes.items()}
    weights = {sym: trend_weights_series(s, c.sma_days) for sym, s in closes_ms.items()}

    def in_window(day: int) -> bool:  # the day's candle closed inside the window
        return since_ms <= day + DAY_MS <= end_ms

    # paper's rebalance trades, against the daily close +/- slippage the backtest fills at
    trades = [t for t in db.core_trades(mode, 1_000_000) if since_ms <= t["ts"] <= end_ms + DAY_MS]
    slip = cfg.costs.slippage_rate
    for t in sorted(trades, key=lambda x: x["ts"]):
        day = last_closed_open_ms(t["ts"] - delay, "1d")
        cm = closes_ms.get(t["symbol"])
        sign = 1 if t["side"] == "buy" else -1
        model_px = float(cm[day]) * (1 + sign * slip) if cm is not None and day in cm.index else None
        value = t["price"] * t["qty"]
        rec.core_trades.append({
            "day": day, "symbol": t["symbol"], "side": t["side"], "reason": t["reason"], "price": t["price"],
            "model_price": model_px, "bps": _bps(t["price"], model_px, sign) if model_px else None,
            "fee_pct": t["fee"] / value * 100 if value else None, "weight_from": t["weight_from"],
            "weight_to": t["weight_to"]})

    # days the bot logged (targets + the state it rebalanced from): same targets? same trades?
    logged = [(ts, d) for ts, d in db.botlog(mode, "core_day", since_ms - DAY_MS) if in_window(int(d["day"]))]
    rec.core_logged_from = min((int(d["day"]) + DAY_MS for _, d in db.botlog(mode, "core_day")), default=None)
    for _, d in logged:
        day = int(d["day"])
        bt_targets = {}
        for sym in c.symbols:
            bw = weights.get(sym, pd.Series(dtype=float)).get(day)
            bt_targets[sym] = None if bw is None or bw != bw else float(bw)
            paper_w = (d.get("targets") or {}).get(sym)
            rec.core_days.append({"day": day, "symbol": sym, "paper": paper_w, "backtest": bt_targets[sym]})
        closes = {sym: float(cm[day]) for sym, cm in closes_ms.items() if day in cm.index}
        planned = rule_trades(cfg, bt_targets, closes, float(d.get("cash") or 0.0), d.get("holdings") or {})
        done = {t["symbol"]: t["side"] for t in rec.core_trades if t["day"] == day and t["reason"] == "trend weights"}
        rec.core_rule_days += 1
        for sym in sorted(set(planned) | set(done)):
            bt_side = planned.get(sym, (None, 0.0))[0]
            if bt_side != done.get(sym):
                rec.core_rule_diffs.append({"day": day, "symbol": sym, "paper": done.get(sym), "backtest": bt_side})
    seen = {int(d["day"]) for _, d in logged}
    if rec.core_logged_from is not None:
        first = max(rec.core_logged_from - DAY_MS, last_closed_open_ms(since_ms, "1d"))
        last = last_closed_open_ms(end_ms - delay, "1d")
        rec.core_days_missing = [d for d in range(first, last + 1, DAY_MS) if in_window(d) and d not in seen]

    # before the log: weight steps the backtest implies, each should have a paper trade within 2 days
    start = None
    for ts, d in db.botlog(mode, "core_transfer"):
        if d.get("why") == "core started":
            start = ts
    first_trade = min((x["day"] for x in rec.core_trades), default=None)
    start_day = last_closed_open_ms(start - delay, "1d") if start else first_trade
    until = rec.core_logged_from - DAY_MS if rec.core_logged_from is not None else None
    for sym, w in weights.items():
        w = w.dropna()
        prev = w.shift(1)
        for day, val in w.items():
            day = int(day)
            if not in_window(day) or prev.get(day) != prev.get(day) or start_day is None or day <= start_day:
                continue
            if until is not None and day >= until:
                continue
            if val != prev[day]:
                side = "buy" if val > prev[day] else "sell"
                match = [x for x in rec.core_trades if x["symbol"] == sym and x["side"] == side
                         and day <= x["day"] <= day + 2 * DAY_MS]
                if not match:
                    rec.core_expected_missing.append({"day": day, "symbol": sym, "side": side,
                                                      "weight_from": float(prev[day]), "weight_to": float(val)})
                elif match[0]["day"] > day:
                    rec.core_late.append({"day": day, "symbol": sym, "side": side, "paper_day": match[0]["day"]})


# --------------------------------------------------------------------- report
def _ts(ms: int | None) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M") if ms else "-"


def _stat(xs: list[float], digits: int = 1) -> str:
    xs = [x for x in xs if x is not None and x == x]
    if not xs:
        return "-"
    a = np.array(xs)
    f = f"+.{digits}f"
    return (f"mean {a.mean():{f}}, median {np.median(a):{f}}, largest {a[np.argmax(np.abs(a))]:{f}} (n={len(a)})")


def format_reconciliation(rec: Reconciliation, cfg: BotConfig, limit: int = 25) -> str:
    rows = rec.rows
    lines = [f"Paper vs backtest reconciliation, {_ts(rec.since_ms)} to {_ts(rec.end_ms)} UTC ({cfg.mode}) - REPORTING ONLY"]
    lines += rec.header
    lines.append(f"  Bot running {rec.uptime_share:.0%} of the window (15-min equity snapshots)"
                 + (": down " + "; ".join(f"{_ts(a)} to {_ts(b)}" for a, b in rec.downs[:6]) if rec.downs else ""))
    if rec.approx_universe_until is None or rec.approx_universe_until > rec.since_ms:
        lines.append(f"  ! Coin list not logged before {_ts(rec.approx_universe_until) if rec.approx_universe_until else 'now'}: "
                     f"approximated there (coins paper signalled + today's list)")
    if rec.approx_selection_until is None or rec.approx_selection_until > rec.since_ms:
        lines.append(f"  ! Active strategies not logged before {_ts(rec.approx_selection_until) if rec.approx_selection_until else 'now'}: "
                     f"today's selection assumed there")
    if rec.ml_active:
        lines.append("  ! The ML filter was active: paper filters signals the backtest cannot reproduce")
    both = [r for r in rows if not r.paper.startswith("(no signal") and not r.backtest.startswith("(")]
    bt_only = [r for r in rows if r.paper.startswith("(no signal")]
    paper_only = [r for r in rows if r.backtest.startswith("(")]
    lines += ["", f"Satellite signals: backtest {len(rows) - len(paper_only)}, paper {len(rows) - len(bt_only)}, "
                  f"both {len(both)}" + ("" if rows else " (none in the window)")]
    if bt_only:
        reasons = pd.Series([r.paper[len("(no signal: "):-1] for r in bt_only]).value_counts()
        lines.append("  Backtest only: " + ", ".join(f"{n} {k}" for k, n in reasons.items()))
    if paper_only:
        none = sum(r.backtest == "(no signal)" for r in paper_only)
        lines.append(f"  Paper only: {len(paper_only)}: {none} the backtest had no signal on that candle"
                     + (f", {len(paper_only) - none} on a coin outside paper's logged list or a strategy not active "
                        f"then (not replayed)" if len(paper_only) > none else ""))
    agree = [r for r in both if (r.backtest == "taken") == r.paper.startswith("opened")]
    if both:
        lines.append(f"  Taken vs skipped agree on {len(agree)} of {len(both)} shared signals")
    disagree = [r for r in both if id(r) not in {id(a) for a in agree}]
    if disagree:
        early = [r for r in disagree if r.backtest == "taken" and r.backtest_exits_at_close != "taken"]
        shadow = [r for r in disagree if r.backtest == SKIPPED_SHADOW]
        lines.append(f"  Of the {len(disagree)} disagreements:")
        if early:
            lines.append(f"    {len(early):>3}  backtest took it only because it frees a slot at the OPEN of the candle in "
                         f"which a trade it holds exits; paper still held that trade (backtest look-ahead)")
        if shadow:
            lines.append(f"    {len(shadow):>3}  backtest was still replaying a trade on that coin/strategy that it had "
                         f"skipped for lack of a slot (backtest simplification; live is free to take the signal)")
        rest = len(disagree) - len(early) - len(shadow)
        if rest:
            lines.append(f"    {rest:>3}  other (often: different open positions after an earlier disagreement)")
        agree2 = [r for r in both if (r.backtest_exits_at_close == "taken") == r.paper.startswith("opened")]
        lines.append(f"  With slots freed at the exit candle's close, as paper does: agree on {len(agree2)} of {len(both)}")
    fills = [r for r in rows if r.entry_bps is not None]
    closed = [r for r in fills if r.exit_paper and r.exit_paper != "still open"]
    if fills:
        lines += ["", f"Fills ({len(fills)} trades both sides entered; + = worse for paper, in basis points):",
                  f"  entry vs modelled fill: {_stat([r.entry_bps for r in fills])}"]
    if closed:
        same_reason = sum(r.exit_paper.split(' @')[0] == r.exit_model.split(' @')[0] for r in closed)
        lines += [f"  exits: same reason {same_reason} of {len(closed)}, same candle "
                  f"{sum(bool(r.exit_same_candle) for r in closed)} of {len(closed)}; exit price vs modelled: "
                  f"{_stat([r.exit_bps for r in closed])}",
                  f"  R paper - R backtest: {_stat([r.r_paper - r.r_model for r in closed])}",
                  f"  fees (% of entry notional): paper {_stat([r.fees_paper_pct for r in closed], 3)}; "
                  f"modelled {_stat([r.fees_model_pct for r in closed], 3)}"]
    notes = [r for r in fills if r.note]
    for r in notes[:limit]:
        lines.append(f"  ! {r.time} {r.symbol} {r.setup}: {r.note}")
    agreed = {id(r) for r in agree}
    diffs = [r for r in rows if id(r) not in agreed]
    if diffs:
        lines += ["", f"Where they differ ({len(diffs)}; times UTC, signal candle close):",
                  f"  {'time':<12}{'coin':<12}{'setup':<16}{'paper':<38}backtest"]
        for r in diffs[:limit]:
            lines.append(f"  {r.time:<12}{r.symbol:<12}{r.setup[:15]:<16}{r.paper[:37]:<38}{r.backtest}")
        if len(diffs) > limit:
            lines.append(f"  ... {len(diffs) - limit} more (--csv for all)")
    # core
    lines += ["", "Core:"]
    if cfg.core.fraction <= 0 and not (rec.core_days or rec.core_trades):
        lines.append("  off in this configuration (core.fraction 0)")
        return "\n".join(lines)
    if rec.core_logged_from is None or rec.core_logged_from > rec.since_ms:
        lines.append(f"  ! Daily targets logged from {_ts(rec.core_logged_from) if rec.core_logged_from else '(not yet)'}; "
                     f"before that only paper's trades can be checked against the weight steps")
    days = [d for d in rec.core_days if d["paper"] is not None or d["backtest"] is not None]  # both None: no history yet
    if days:
        same = [d for d in days if d["backtest"] is not None and d["paper"] is not None
                and abs(d["paper"] - d["backtest"]) < 1e-9]
        lines.append(f"  Daily target weights: {len(same)} of {len(days)} coin-days match the backtest")
        for d in [d for d in days if not any(d is x for x in same)][:limit]:
            lines.append(f"    ! {_ts(d['day'])[:10]} {d['symbol']}: paper {d['paper']} vs backtest {d['backtest']}")
    if rec.core_rule_days:
        lines.append(f"  Rebalance rule replayed from paper's own state on {rec.core_rule_days} logged days "
                     f"(backtest targets, daily closes): {len(rec.core_rule_diffs)} coin-days trade differently")
        for m in rec.core_rule_diffs[:limit]:
            lines.append(f"    ! {_ts(m['day'])[:10]} {m['symbol']}: paper {m['paper'] or 'no trade'}, "
                         f"backtest {m['backtest'] or 'no trade'}")
    if rec.core_days_missing:
        lines.append(f"  ! {len(rec.core_days_missing)} days with no core check (bot not running at the day change): "
                     + ", ".join(_ts(d)[:10] for d in rec.core_days_missing[:10]))
    if rec.core_trades:
        steps = [t for t in rec.core_trades if t["reason"] == "trend weights" and t["weight_from"] != t["weight_to"]]
        drift = [t for t in rec.core_trades if t["reason"] == "trend weights" and t["weight_from"] == t["weight_to"]]
        other = [t for t in rec.core_trades if t["reason"] != "trend weights"]
        lines += [f"  Trades: {len(steps)} on weight steps, {len(drift)} drift, {len(other)} sleeve moves",
                  f"    fill vs daily close +/- slippage (bps, + = worse for paper): "
                  f"{_stat([t['bps'] for t in rec.core_trades])}",
                  f"    fees (% of value): {_stat([t['fee_pct'] for t in rec.core_trades], 3)}; "
                  f"modelled {cfg.costs.fee_rate * 100:.3f}"]
    if rec.core_expected_missing:
        lines.append(f"  Before the log: weight steps the backtest implies with no paper trade within 2 days: "
                     f"{len(rec.core_expected_missing)}")
        for m in rec.core_expected_missing[:limit]:
            lines.append(f"    ! {_ts(m['day'])[:10]} {m['symbol']} {m['side']} ({m['weight_from']:.2f} -> {m['weight_to']:.2f})")
    for m in rec.core_late[:limit]:
        lines.append(f"    ! late: {_ts(m['day'])[:10]} {m['symbol']} {m['side']} traded by paper on {_ts(m['paper_day'])[:10]}")
    if not (rec.core_days or rec.core_trades):
        lines.append("  no core activity in the window")
    return "\n".join(lines)


def write_csv(rec: Reconciliation, path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(asdict(rec.rows[0]).keys()) if rec.rows else ["time"])
        w.writeheader()
        for r in rec.rows:
            w.writerow(asdict(r))
