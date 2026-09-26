"""Offline end-to-end demo on synthetic data.

1. learn: select strategies + train the ML filter on ~1.5 years of fake history
2. simulate: run the real bot loop over the following weeks, candle by candle,
   paper trading with Telegram-style messages captured in memory
3. report: track record + go-live readiness

This proves the machinery works. Synthetic prices are NOT real markets, so the
numbers say nothing about how the bot will do with real money.
"""
from __future__ import annotations

import logging
import re
import shutil
import time
from pathlib import Path

from .bot import TradingBot
from .config import BotConfig
from .db import Database
from .execution import PaperBroker
from .learning import learning_cycle
from .notify import MemoryNotifier
from .notify import formatting as fmt
from .report import format_readiness, readiness


def _plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def run_demo(days: int = 540, sim_days: int = 45, symbols: int = 6, seed: int = 7,
             state_dir: str = "state/demo", out=print) -> dict:
    from .data import SyntheticMarket

    state = Path(state_dir)
    if state.exists():
        shutil.rmtree(state)
    cfg = BotConfig(state_dir=str(state), timeframes=["1h", "4h"])
    cfg.data.history_days = {"1h": days, "4h": days}
    cfg.universe.top_n = symbols
    cfg.telegram.daily_summary_hour_utc = 0

    market = SyntheticMarket(symbols, days=days + sim_days + 2, seed=seed)
    end = market.now_ms()
    learn_until = end - sim_days * 86_400_000
    market.set_now(learn_until)

    out("=" * 72)
    out(" DEMO on SYNTHETIC data - proves the machinery, NOT real-market profits")
    out("=" * 72)
    out(f"\n[1/3] Self-learning on {days} days of history for {symbols} symbols\n")
    t0 = time.time()
    res = learning_cycle(cfg, market, now_ms=learn_until, log_fn=out)
    out(f"\n{res.summary}\n  ({time.time() - t0:.0f}s)")
    if res.selection.selected:
        from .learning import load_context, load_datasets
        from .projection import format_projection, out_of_sample_trades, project

        datasets = load_datasets(market, cfg, market.symbols, None, learn_until, log_fn=lambda *_: None)
        context = load_context(market, cfg, None, learn_until, log_fn=lambda *_: None)
        trades = out_of_sample_trades(res.selection, datasets, cfg, context)
        if trades:
            proj = project(trades, cfg, capital=1000.0, benchmark=datasets["1h"].get("BTC/USDT"),
                           benchmark_symbol="BTC/USDT")
            out("\n" + "!" * 72 + "\n SYNTHETIC PRICES - the synthetic market trends far more cleanly than real crypto,\n"
                " so these numbers are NOT a forecast. Run `tradebot learn` + `tradebot project` on\n"
                " real data for a meaningful projection.\n" + "!" * 72)
            out(format_projection(proj))

    out(f"\n[2/3] Paper trading the next {sim_days} days candle-by-candle with the live bot loop...\n")
    db = Database(state / "tradebot.db")
    broker = PaperBroker(db, cfg.costs_model(), 1000.0, market)
    notifier = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notifier, selection=res.selection, model=res.model)
    logging.getLogger("tradebot").setLevel(logging.WARNING)  # keep the demo output readable
    step = market.price_bar_ms
    t = (learn_until // step + 1) * step
    t0 = time.time()
    next_report = t + 10 * 86_400_000
    while t <= end:
        now = t + 30_000  # 30s after each candle closes
        market.set_now(now)
        bot.tick(now)
        t += step
        if t >= next_report:
            next_report += 10 * 86_400_000
            eq, _ = bot.equity()
            out(f"  day {(t - learn_until) / 86_400_000:>3.0f}: {len(db.closed_positions('paper'))} trades closed, "
                f"{len(db.open_positions('paper'))} open, equity {eq:,.2f} USDT")
    out(f"  simulated {sim_days} days in {time.time() - t0:.0f}s")

    msgs = notifier.messages
    entries = [m for m in msgs if "SIGNAL" in m]
    exits = [m for m in msgs if " NOW " in m]
    moves = [m for m in msgs if "breakeven" in m.lower() and "Move stop" in m]
    out(f"  messages sent: {len(entries)} trade signals, {len(exits)} exit alerts, {len(moves)} stop moves\n")
    for label, sample in (("Example signal", entries[:1]), ("Example stop move", moves[:1]), ("Example exit", exits[:1])):
        for m in sample:
            out(f"--- {label} (what you'd get on Telegram) ---\n{_plain(m)}\n")

    out("[3/3] Results\n")
    closed = db.closed_positions("paper")
    out(_plain(fmt.format_performance(closed, cfg.exchange.quote, "Paper track record (synthetic)")))
    equity, _ = bot.equity()
    out(f"Equity: {equity:,.2f} USDT (started with 1,000.00)\n")
    out("Go-live readiness checklist:")
    out(format_readiness(readiness(db, cfg, end)))
    out("\nReminder: synthetic data. Run `tradebot learn` + `tradebot run` against a real exchange "
        "in paper mode to build a real track record.")
    return {"signals": len(entries), "exits": len(exits), "closed": len(closed), "equity": equity}
