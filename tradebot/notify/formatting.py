"""Human-friendly (Telegram HTML) messages."""
from __future__ import annotations

import html
import math

import pandas as pd

from ..models import Position, Signal

REASONS = {
    "take_profit": "Take-profit hit",
    "stop_loss": "Stop-loss hit",
    "breakeven_stop": "Stopped out at breakeven",
    "trailing_stop": "Trailing stop hit - gains locked in",
    "exit_signal": "Exit signal (setup invalidated)",
    "time_stop": "Time limit reached",
    "manual": "Closed manually",
    "kill_switch": "Closed by /closeall",
    "no_protection": "Closed for safety: no exchange stop-loss could be placed",
    "stop_order_executed": "The exchange executed the stop order immediately",
    "forgotten": "Removed with /forget (no trade was made)",
}


def fmt_price(p: float | None) -> str:
    if p is None or (isinstance(p, float) and math.isnan(p)):
        return "-"
    ap = abs(p)
    decimals = 2 if ap >= 1000 else 3 if ap >= 100 else 4 if ap >= 1 else 6 if ap >= 0.01 else 8
    return f"{p:,.{decimals}f}"


def fmt_money(x: float, quote: str = "USDT") -> str:
    return f"{x:+,.2f} {quote}" if x else f"0.00 {quote}"


def fmt_time(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")


def esc(s: str) -> str:
    return html.escape(str(s), quote=False)


def format_signal(sig: Signal, pos: Position | None, equity: float, mode: str, quote: str,
                  max_chase_r: float, ml_active: bool) -> str:
    buy = sig.side == "long"
    head = "🟢 <b>BUY" if buy else "🔴 <b>SELL (short)"
    conf = f"{sig.confidence:.0%}" if sig.confidence is not None else "n/a (ML filter not active yet)"
    entry = pos.entry_price if pos is not None else sig.entry
    risk = abs(entry - sig.stop_loss)
    rr = abs(sig.take_profit - entry) / risk if risk > 0 else 0.0
    working = pos is not None and pos.status == "working"
    entry_lines = [
        f"Entry:        <code>{fmt_price(entry)}</code>  (limit {'buy' if buy else 'sell'} - fills only if price "
        f"trades {'down' if buy else 'up'} through it before {fmt_time(pos.limit_until)})",
    ] if working else [
        f"Entry:        <code>{fmt_price(entry)}</code>  (market, now)",
        f"Don't chase:  {'above' if buy else 'below'} <code>{fmt_price(sig.chase_limit(max_chase_r))}</code>",
    ]
    lines = [
        f"{head}{' LIMIT' if working else ''} SIGNAL — {esc(sig.symbol)}</b> ({sig.timeframe})",
        f"Strategy: {esc(sig.strategy)} · Win probability: {conf}",
        "",
        *entry_lines,
        f"Stop-loss:    <code>{fmt_price(sig.stop_loss)}</code>  ({(sig.stop_loss / entry - 1) * 100:+.2f}%)",
        f"Take-profit:  <code>{fmt_price(sig.take_profit)}</code>  ({(sig.take_profit / entry - 1) * 100:+.2f}%)"
        + ("  (far target - a trailing stop usually exits first)" if sig.trail_distance else ""),
        f"Reward:risk:  1:{rr:.1f}",
    ]
    if sig.trail_distance:
        lines.append(f"Trailing:     once +1R, stop follows {fmt_price(sig.trail_distance)} behind the best price")
    if pos is not None:
        risk_pct = pos.initial_risk / equity * 100 if equity > 0 else 0.0
        base = sig.symbol.split("/")[0]
        lines.append(
            f"Size:         {pos.amount:.6g} {esc(base)} (~{pos.notional:,.2f} {quote}), "
            f"risking {pos.initial_risk:,.2f} {quote} ({risk_pct:.1f}%)"
            + (f" - high-confidence setup, {sig.risk_multiplier:.1f}x normal risk" if sig.risk_multiplier > 1.05 else "")
        )
    lines += [
        f"Time limit:   close by {fmt_time(sig.max_hold_until)} if neither level is hit",
        "",
        f"<i>Why:</i> {esc(sig.reason)}",
        f"#{pos.id if pos is not None and pos.id else sig.id} · {mode} mode"
        + ("" if ml_active else " · rules only"),
    ]
    return "\n".join(lines)


def format_exit(pos: Position, quote: str) -> str:
    win = (pos.pnl or 0) > 0
    icon = "✅" if win else "🛑" if pos.exit_reason == "stop_loss" else "⚪"
    action = "SELL" if pos.side == "long" else "BUY BACK"
    pct = pos.sign * (pos.exit_price / pos.entry_price - 1) * 100 if pos.exit_price else 0.0
    return "\n".join([
        f"{icon} <b>{action} NOW — {esc(pos.symbol)}</b> #{pos.id}",
        f"Reason: {REASONS.get(pos.exit_reason or '', esc(pos.exit_reason or ''))}",
        f"Entry <code>{fmt_price(pos.entry_price)}</code> → Exit <code>{fmt_price(pos.exit_price)}</code>",
        f"Result: {pct:+.2f}% · {pos.r_multiple or 0:+.2f}R · P&amp;L {fmt_money(pos.pnl or 0, quote)} ({pos.mode})",
    ])


def format_stop_move(pos: Position) -> str:
    return (
        f"🔒 <b>Move stop to breakeven — {esc(pos.symbol)}</b> #{pos.id}\n"
        f"Price reached +1R. New stop-loss: <code>{fmt_price(pos.stop_loss)}</code> (your entry). "
        f"This trade can no longer lose money (except fees/slippage)."
    )


def format_trail_move(pos: Position) -> str:
    locked = pos.r_at(pos.stop_loss)
    return (
        f"📈 <b>Raise stop — {esc(pos.symbol)}</b> #{pos.id}\n"
        f"New trailing stop: <code>{fmt_price(pos.stop_loss)}</code> (locks in {locked:+.1f}R if hit)."
    )


def format_core_rebalance(trades, weights: dict, core_equity: float, quote: str) -> str:
    lines = [f"⚖️ <b>Core rebalance</b> (daily close) · core equity {core_equity:,.2f} {quote}"]
    for t in trades:
        base = t.symbol.split("/")[0]
        lines.append(f"{'Bought' if t.side == 'buy' else 'Sold'} {t.qty:.6g} {esc(base)} @ {fmt_price(t.price)} "
                     f"(~{t.qty * t.price:,.2f} {quote}) · weight {t.weight_from:.0%} → {t.weight_to:.0%}")
    if weights:
        lines.append("Targets: " + ", ".join(f"{esc(k.split('/')[0])} {v:.0%}" for k, v in weights.items()))
    return "\n".join(lines)


def format_positions(positions: list[Position], prices: dict[str, float], quote: str) -> str:
    if not positions:
        return "No open positions."
    out = ["<b>Open positions</b>"]
    for p in positions:
        px = prices.get(p.symbol)
        upnl = p.unrealized(px) if px else 0.0
        out.append(
            f"#{p.id} {esc(p.symbol)} {p.side} {p.timeframe} · entry {fmt_price(p.entry_price)} · now {fmt_price(px)}\n"
            f"   SL {fmt_price(p.stop_loss)} · TP {fmt_price(p.take_profit)} · "
            f"{p.r_at(px) if px else 0:+.2f}R · {fmt_money(upnl, quote)}"
        )
    return "\n".join(out)


def format_performance(closed: list[Position], quote: str, title: str = "Performance") -> str:
    if not closed:
        return f"<b>{title}</b>\nNo closed trades yet."
    rs = [p.r_multiple or 0 for p in closed]
    pnl = sum(p.pnl or 0 for p in closed)
    wins = [r for r in rs if r > 0]
    gross_loss = -sum(r for r in rs if r <= 0)
    pf = sum(wins) / gross_loss if gross_loss > 0 else float("inf")
    return "\n".join([
        f"<b>{title}</b>",
        f"Trades: {len(rs)} · Win rate: {len(wins) / len(rs):.0%}",
        f"Avg: {sum(rs) / len(rs):+.2f}R per trade · Profit factor: {pf:.2f}",
        f"Total: {sum(rs):+.1f}R · P&amp;L {fmt_money(pnl, quote)}",
    ])
