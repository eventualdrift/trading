"""Bar-by-bar trade simulation shared by backtests, ML labelling and selection.

Execution model (kept deliberately conservative):
  * a signal is generated on the close of bar i; the order fills at the open of
    bar i+1 plus slippage;
  * if bar i+1 already gaps past the stop or target, the trade is skipped;
  * if the stop and target are both inside the same bar, the STOP wins;
  * a stop that gaps is filled at the (worse) open price;
  * the take-profit is a market sell the bot sends when it SEES the price at the
    target. A bar's high may be a brief wick the bot never saw, so the target only
    counts when the bar closes at or beyond it (filled at the target less slippage).
    This is deliberately pessimistic: live, the bot also catches some wicks;
  * breakeven: once a bar trades +1R the stop moves to entry for later bars, and if
    that same bar closes back at/below entry the trade exits at the close (the close
    is observed after the high);
  * fees are charged on both entry and exit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..strategies.base import Strategy


@dataclass(frozen=True)
class Costs:
    fee_rate: float = 0.001
    slippage_rate: float = 0.0005


@dataclass
class TradeOutcome:
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    reason: str
    r_multiple: float
    return_pct: float  # net return on notional as a fraction (0.01 = +1%)
    complete: bool  # False if the data ran out before the trade finished


@dataclass
class Trade:
    symbol: str
    timeframe: str
    strategy: str
    side: str
    signal_idx: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    reason: str
    bars_held: int
    r_multiple: float
    return_pct: float
    stop_pct: float  # initial risk as a fraction of entry price

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("signal_time", "entry_time", "exit_time"):
            d[k] = pd.Timestamp(d[k]).isoformat()
        return d


def net_return(side: str, entry: float, exit_: float, fee: float) -> float:
    if side == "long":
        return (exit_ * (1 - fee) - entry * (1 + fee)) / entry
    return (entry * (1 - fee) - exit_ * (1 + fee)) / entry


def simulate_trade(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    exit_flags: np.ndarray | None,
    signal_idx: int,
    side: str,
    sl: float,
    tp: float,
    max_hold: int,
    costs: Costs,
    breakeven_at_r: float = 0.0,
) -> TradeOutcome | None:
    n = len(c)
    e = signal_idx + 1
    if e >= n:
        return None
    long = side == "long"
    sign = 1.0 if long else -1.0
    slip, fee = costs.slippage_rate, costs.fee_rate

    raw = o[e]
    if long and not (sl < raw < tp):
        return None
    if not long and not (tp < raw < sl):
        return None
    entry = raw * (1 + sign * slip)
    risk = abs(entry - sl)
    if risk <= 0 or (long and entry <= sl) or (not long and entry >= sl):
        return None

    stop = sl
    be_level = entry + sign * breakeven_at_r * risk if breakeven_at_r > 0 else None
    at_breakeven = False
    last = min(n - 1, e + max_hold - 1)

    exit_idx, exit_price, reason = last, c[last], ""
    for j in range(e, last + 1):
        stop_reason = "breakeven_stop" if at_breakeven else "stop_loss"
        if long:
            if j > e and o[j] <= stop:
                exit_idx, exit_price, reason = j, o[j] * (1 - slip), stop_reason
                break
            if l[j] <= stop:
                exit_idx, exit_price, reason = j, stop * (1 - slip), stop_reason
                break
            if c[j] >= tp:
                exit_idx, exit_price, reason = j, tp * (1 - slip), "take_profit"
                break
        else:
            if j > e and o[j] >= stop:
                exit_idx, exit_price, reason = j, o[j] * (1 + slip), stop_reason
                break
            if h[j] >= stop:
                exit_idx, exit_price, reason = j, stop * (1 + slip), stop_reason
                break
            if c[j] <= tp:
                exit_idx, exit_price, reason = j, tp * (1 + slip), "take_profit"
                break
        if exit_flags is not None and exit_flags[j]:
            exit_idx, exit_price, reason = j, c[j] * (1 - sign * slip), "exit_signal"
            break
        if be_level is not None and not at_breakeven:
            if (long and h[j] >= be_level) or (not long and l[j] <= be_level):
                stop = entry  # intrabar checks apply from the next bar on
                at_breakeven = True
                if (c[j] <= entry) if long else (c[j] >= entry):
                    exit_idx, exit_price, reason = j, c[j] * (1 - sign * slip), "breakeven_stop"
                    break

    complete = True
    if not reason:
        if last == e + max_hold - 1:
            reason = "time_stop"
            exit_price = c[last] * (1 - sign * slip)
        else:
            reason, complete = "end_of_data", False
            exit_price = c[last]

    ret = net_return(side, entry, exit_price, fee)
    return TradeOutcome(
        entry_idx=e,
        exit_idx=exit_idx,
        entry_price=float(entry),
        exit_price=float(exit_price),
        reason=reason,
        r_multiple=float(ret * entry / risk),
        return_pct=float(ret),
        complete=complete,
    )


def reward_risk(side: str, close: float, sl: float, tp: float) -> float:
    risk = (close - sl) if side == "long" else (sl - close)
    reward = (tp - close) if side == "long" else (close - tp)
    return reward / risk if risk > 0 else 0.0


def backtest_populated(
    pop: pd.DataFrame,
    strategy: Strategy,
    costs: Costs,
    *,
    symbol: str = "",
    timeframe: str = "",
    allow_short: bool = False,
    breakeven_at_r: float = 0.0,
    min_reward_risk: float = 0.0,
    start_idx: int | None = None,
    end_idx: int | None = None,
) -> list[Trade]:
    """Sequential (one position at a time) backtest over an already-populated frame."""
    o, h, l, c = (pop[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    el, es = pop["enter_long"].to_numpy(), pop["enter_short"].to_numpy()
    xl, xs = pop["exit_long"].to_numpy(), pop["exit_short"].to_numpy()
    lsl, ltp = pop["long_sl"].to_numpy(dtype=float), pop["long_tp"].to_numpy(dtype=float)
    ssl, stp = pop["short_sl"].to_numpy(dtype=float), pop["short_tp"].to_numpy(dtype=float)
    idx = pop.index
    n = len(pop)
    i = strategy.warmup if start_idx is None else max(start_idx, strategy.warmup)
    end = n - 1 if end_idx is None else min(end_idx, n - 1)
    trades: list[Trade] = []
    while i < end:
        go_long = bool(el[i])
        go_short = bool(es[i]) and allow_short
        if go_long == go_short:  # neither, or conflicting signals
            i += 1
            continue
        side = "long" if go_long else "short"
        sl, tp = (lsl[i], ltp[i]) if go_long else (ssl[i], stp[i])
        if reward_risk(side, c[i], sl, tp) < min_reward_risk:
            i += 1
            continue
        out = simulate_trade(
            o, h, l, c, xl if go_long else xs, i, side, sl, tp,
            strategy.max_hold_bars, costs, breakeven_at_r,
        )
        if out is None:
            i += 1
            continue
        trades.append(
            Trade(
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy.name,
                side=side,
                signal_idx=i,
                signal_time=idx[i],
                entry_time=idx[out.entry_idx],
                exit_time=idx[out.exit_idx],
                entry_price=out.entry_price,
                exit_price=out.exit_price,
                stop_loss=float(sl),
                take_profit=float(tp),
                reason=out.reason,
                bars_held=out.exit_idx - out.entry_idx + 1,
                r_multiple=out.r_multiple,
                return_pct=out.return_pct,
                stop_pct=abs(out.entry_price - sl) / out.entry_price,
            )
        )
        i = max(out.exit_idx, i + 1)
    return trades


def backtest(
    df: pd.DataFrame, strategy: Strategy, costs: Costs, **kwargs
) -> tuple[list[Trade], pd.DataFrame]:
    pop = strategy.populate(df)
    return backtest_populated(pop, strategy, costs, **kwargs), pop


def portfolio_simulation(
    trades: list[Trade],
    *,
    risk_per_trade_pct: float,
    max_position_pct: float,
    max_open_positions: int,
    start_equity: float = 1000.0,
) -> tuple[pd.Series, list[Trade]]:
    """Replay trades from many symbols as one account with position limits.

    Position size = risk% of equity / stop distance, capped at max_position_pct of
    equity, exactly like the live risk manager. Returns (equity curve, trades taken).
    """
    equity = start_equity
    curve = {}
    open_: list[tuple[Trade, float]] = []  # (trade, pnl)
    taken: list[Trade] = []
    for t in sorted(trades, key=lambda t: (t.entry_time, t.symbol)):
        still_open = []
        for ot, pnl in sorted(open_, key=lambda x: x[0].exit_time):
            if ot.exit_time <= t.entry_time:
                equity += pnl
                curve[ot.exit_time] = equity
            else:
                still_open.append((ot, pnl))
        open_ = still_open
        if len(open_) >= max_open_positions or any(ot.symbol == t.symbol for ot, _ in open_):
            continue
        frac = min(risk_per_trade_pct / 100.0 / max(t.stop_pct, 1e-9), max_position_pct / 100.0)
        open_.append((t, equity * frac * t.return_pct))
        taken.append(t)
    for ot, pnl in sorted(open_, key=lambda x: x[0].exit_time):
        equity += pnl
        curve[ot.exit_time] = equity
    series = pd.Series(curve, dtype=float).sort_index()
    if not series.empty:
        series = series.groupby(level=0).last()
    return series, taken
