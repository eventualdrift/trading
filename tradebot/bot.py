"""The trading bot: watches the market 24/7, sends signals, manages positions."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import pandas as pd

from .backtest.selection import Selection
from .config import BotConfig
from .db import Database
from .execution.base import Broker, NotFilled, finalize_close
from .ml.model import SignalModel
from .models import Position, Signal
from .notify import formatting as fmt
from .risk import RiskManager
from .scanner import Scanner
from .timeframes import index_ms, last_closed_open_ms, tf_ms

log = logging.getLogger(__name__)

HELP = """<b>Commands</b>
/status – mode, equity, what the bot is trading
/positions – open positions with live P&amp;L
/signals – latest signals
/performance – track record
/pause – stop opening new trades (open trades are still managed)
/resume – resume trading (also clears a drawdown halt)
/close &lt;id&gt; – close one position at market
/closeall – close everything and pause
/forget &lt;id&gt; – mark a position closed WITHOUT trading (after fixing it on the exchange yourself)
/learn – run the self-learning cycle now"""

Learner = Callable[[SignalModel | None], "tuple[Selection, SignalModel | None, str]"]


class TradingBot:
    def __init__(
        self,
        cfg: BotConfig,
        market,
        broker: Broker,
        db: Database,
        notifier,
        *,
        selection: Selection | None = None,
        model: SignalModel | None = None,
        learner: Learner | None = None,
    ):
        self.cfg = cfg
        self.market = market
        self.broker = broker
        self.db = db
        self.notifier = notifier
        self.mode = broker.mode
        self.risk = RiskManager(cfg.risk)
        self.learner = learner
        self.selection = selection
        self.model = model
        self.scanner = Scanner(market, cfg, selection, model)
        self._symbols: list[str] = []
        self._symbols_at = 0
        self._lock = threading.RLock()
        self._learn_thread: threading.Thread | None = None
        self._last_error_notice: dict[str, float] = {}
        self._last_equity_record = 0
        self._stop = threading.Event()

    # ----------------------------------------------------------------- state
    def _k(self, key: str) -> str:
        return f"{self.mode}:{key}"

    def _get(self, key: str, default=None):
        return self.db.kv_get(self._k(key), default)

    def _set(self, key: str, value) -> None:
        self.db.kv_set(self._k(key), value)

    @property
    def ml_active(self) -> bool:
        return self.scanner.model is not None

    def set_brain(self, selection: Selection | None, model: SignalModel | None) -> None:
        with self._lock:
            if selection is not None:
                self.selection = selection
            if model is not None:
                self.model = model
            self.scanner = Scanner(self.market, self.cfg, self.selection, self.model)

    def active_timeframes(self) -> list[str]:
        return self.selection.timeframes() if self.selection else []

    def universe(self, now_ms: int) -> list[str]:
        u = self.cfg.universe
        if not self._symbols or now_ms - self._symbols_at > u.refresh_hours * 3_600_000:
            try:
                self._symbols = self.market.top_symbols(
                    self.cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist
                )
                self._symbols_at = now_ms
            except Exception as exc:
                log.warning("universe refresh failed: %s", exc)
        return self._symbols

    def notify(self, text: str) -> None:
        try:
            self.notifier.send(text)
        except Exception as exc:
            log.warning("notification failed: %s", exc)

    def _notify_error(self, text: str, key: str = "general") -> None:
        """Log every error; message the user at most every 30 min per ``key``."""
        log.error(text)
        if time.time() - self._last_error_notice.get(key, 0.0) > 1800:
            self._last_error_notice[key] = time.time()
            self.notify(f"⚠️ <b>Bot error</b>\n{fmt.esc(text)[:1500]}")

    def _halt(self, reason: str) -> None:
        if not self._get("halted"):
            self._set("halted", reason)
            self.notify(f"🚨 <b>New entries halted</b>: {fmt.esc(reason)}\nOpen trades are still managed. "
                        f"Check your exchange account, then send /resume.")

    # --------------------------------------------------------------- equity
    def equity(self) -> tuple[float, dict[str, float]]:
        positions = self.db.open_positions(self.mode)
        prices = {}
        for p in positions:
            try:
                prices[p.symbol] = self.market.fetch_last_price(p.symbol)
            except Exception as exc:
                log.warning("price for %s unavailable: %s", p.symbol, exc)
        return self.broker.equity(prices, positions), prices

    def _update_breakers(self, now_ms: int, equity: float) -> None:
        day = pd.Timestamp(now_ms, unit="ms", tz="UTC").date().isoformat()
        if self._get("day") != day:
            self._set("day", day)
            self._set("day_start_equity", equity)
            self._set("daily_limit_notified", False)
        peak = max(float(self._get("peak_equity", equity)), equity)
        self._set("peak_equity", peak)
        if not self._get("halted") and self.risk.drawdown_hit(equity, peak):
            reason = f"drawdown {(1 - equity / peak) * 100:.1f}% from peak {peak:,.2f}"
            self._set("halted", reason)
            self.notify(f"🚨 <b>Trading halted</b>: {reason}.\nOpen trades are still managed. "
                        f"Review performance, then send /resume to continue.")
        if not self._get("daily_limit_notified") and self.risk.daily_limit_hit(equity, self._get("day_start_equity", equity)):
            self._set("daily_limit_notified", True)
            self.notify(f"⚠️ Daily loss limit ({self.cfg.risk.daily_loss_limit_pct}%) reached - "
                        f"no new trades until 00:00 UTC.")
        if now_ms - self._last_equity_record >= 15 * 60_000:
            self._last_equity_record = now_ms
            self.db.record_equity(now_ms, self.mode, equity)

    def _entry_block(self, sig: Signal, open_positions: list[Position], equity: float) -> str | None:
        if self._get("paused"):
            return "paused by user"
        if self._get("halted"):
            return f"halted ({self._get('halted')})"
        if self.risk.daily_limit_hit(equity, self._get("day_start_equity", equity)):
            return "daily loss limit reached"
        return self.risk.entry_block_reason(sig, open_positions)

    # ------------------------------------------------------------ main loop
    def run_forever(self) -> None:
        tfs = ", ".join(self.active_timeframes()) or "none"
        combos = ", ".join(c.key for c in (self.selection.selected if self.selection else [])) or "none"
        self.notify(
            f"🤖 <b>tradebot started</b> ({self.mode} mode)\nStrategies: {fmt.esc(combos)}\nTimeframes: {tfs}\n"
            f"ML filter: {'active' if self.ml_active else 'not active'}\nSend /help for commands."
        )
        listener = getattr(self.notifier, "start_listener", None)
        if callable(listener):
            listener(self.handle_command)
        try:
            self.check_unresolved()
        except Exception as exc:
            self._notify_error(f"start-up reconciliation failed: {exc}")
        while not self._stop.is_set():
            try:
                self.tick(self.market.now_ms())
            except Exception as exc:
                log.exception("tick failed")
                self._notify_error(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.cfg.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def tick(self, now_ms: int) -> None:
        with self._lock:
            self.manage_positions(now_ms)
            equity, _ = self.equity()
            self._update_breakers(now_ms, equity)
            delay = int(self.cfg.candle_close_delay_seconds * 1000)
            for tf in self.active_timeframes():
                candle = last_closed_open_ms(now_ms - delay, tf)
                if candle <= self._get(f"last_scan:{tf}", 0):
                    continue
                age = now_ms - (candle + tf_ms(tf))
                if age <= max(4 * delay, min(tf_ms(tf) // 4, 3_600_000)):
                    self.on_candle_close(tf, now_ms, candle)
                else:
                    log.info("skipping stale %s candle (bot started late)", tf)
                self._set(f"last_scan:{tf}", candle)
            self._maybe_daily_summary(now_ms, equity)
        self._maybe_learn(now_ms)

    def on_candle_close(self, tf: str, now_ms: int, candle_open_ms: int) -> None:
        symbols = self.universe(now_ms)
        open_positions = self.db.open_positions(self.mode)
        res = self.scanner.scan(tf, symbols, now_ms, candle_open_ms, open_positions)
        for err in res.errors:
            log.warning("scan: %s", err)
        for pid, reason in res.exits.items():
            pos = self.db.get_position(pid)
            if pos and pos.status == "open":
                self._close(pos, self.market.fetch_last_price(pos.symbol), reason, now_ms)
        for sig in res.filtered:
            self.db.insert_signal(sig)
        if res.accepted:
            equity, _ = self.equity()
            for sig in res.accepted:
                self.handle_signal(sig, now_ms, equity)
        level = logging.INFO if (res.accepted or res.filtered or res.exits) else logging.DEBUG
        log.log(level, "%s scan: %d symbols, %d signals, %d filtered by ML, %d exits",
                tf, len(symbols), len(res.accepted), len(res.filtered), len(res.exits))

    def handle_signal(self, sig: Signal, now_ms: int, equity: float) -> Position | None:
        open_positions = self.db.open_positions(self.mode)
        block = self._entry_block(sig, open_positions, equity)
        price, size = None, None
        if block is None:
            price = self.market.fetch_last_price(sig.symbol)
            limit = sig.chase_limit(self.cfg.risk.max_chase_r)
            beyond = price >= limit if sig.side == "long" else price <= limit
            invalid = (price <= sig.stop_loss or price >= sig.take_profit) if sig.side == "long" \
                else (price >= sig.stop_loss or price <= sig.take_profit)
            if beyond or invalid:
                block = f"price {fmt.fmt_price(price)} already outside the entry zone"
        if block is None:
            size = self.risk.size(
                equity, price, sig.stop_loss,
                open_notional=sum(p.notional for p in open_positions),
                available_cash=self.broker.available_cash(),
                limits=self.broker.limits(sig.symbol),
                to_precision=self.broker.to_precision(sig.symbol),
            )
            if not size.ok:
                block = size.reason
                if "minimum" in block and self._get("min_size_notified") != pd.Timestamp(now_ms, unit="ms").date().isoformat():
                    self._set("min_size_notified", pd.Timestamp(now_ms, unit="ms").date().isoformat())
                    self.notify(f"ℹ️ Skipped {fmt.esc(sig.symbol)} signal: {fmt.esc(block)}")
        if block is not None:
            sig.status, sig.note = "skipped", block
            self.db.insert_signal(sig)
            log.info("skipped %s %s: %s", sig.symbol, sig.strategy, block)
            return None

        sig.status = "opened"
        self.db.insert_signal(sig)
        pos = Position.from_signal(sig, self.mode, size.amount, now_ms)
        self.db.insert_position(pos)  # recorded BEFORE any order is sent
        try:
            self.broker.open_position(pos, price, now_ms)
        except NotFilled as exc:
            pos.status, pos.exit_reason = "failed", str(exc)[:300]
            self.db.update_position(pos)
            sig.status, sig.note = "skipped", f"entry failed: {exc}"
            self.db.update_signal(sig)
            self._notify_error(f"Entry for {sig.symbol} failed (nothing was bought): {exc}", key="entry")
            return None
        except Exception as exc:
            pos.status, pos.exit_reason = "unknown", str(exc)[:300]
            self.db.update_position(pos)
            self._halt(f"entry #{pos.id} for {sig.symbol} has an unknown outcome ({exc})")
            return None
        bar = self.market.price_bar_ms
        pos.last_checked_ms = (now_ms // bar + 1) * bar
        self.db.update_position(pos)
        self._protect(pos, now_ms)
        if pos.status == "open":
            self.notify(fmt.format_signal(sig, pos, equity, self.mode, self.cfg.exchange.quote,
                                          self.cfg.risk.max_chase_r, self.ml_active))
        return pos

    def _protect(self, pos: Position, now_ms: int) -> None:
        try:
            self.broker.protect(pos, now_ms)
        except Exception as exc:
            self.db.update_position(pos)
            self._unprotected(pos, str(exc), now_ms)
            return
        self.db.update_position(pos)
        if pos.status == "closed":  # the "stop" executed straight away
            self.notify(fmt.format_exit(pos, self.cfg.exchange.quote))

    def _unprotected(self, pos: Position, why: str, now_ms: int) -> None:
        if pos.status != "open":
            return
        sym = fmt.esc(pos.symbol)
        if self.cfg.live.require_exchange_stop:
            self.notify(f"🚨 <b>No exchange stop-loss for {sym}</b> #{pos.id}: {fmt.esc(why)}\n"
                        f"Closing the position for safety.")
            self._close(pos, self._price(pos), "no_protection", now_ms)
        else:
            self.notify(f"⚠️ No exchange stop-loss for {sym} #{pos.id}: {fmt.esc(why)}\n"
                        f"The bot enforces the stop itself - but only while it is running.")

    def _price(self, pos: Position) -> float:
        try:
            return self.market.fetch_last_price(pos.symbol)
        except Exception:
            return pos.entry_price

    # ---------------------------------------------------- position management
    def check_unresolved(self) -> None:
        """At start-up: surface entries whose outcome was never confirmed and make
        sure every live position is protected."""
        for pos in self.db.positions_with_status(self.mode, ("pending", "unknown")):
            if self.mode == "paper":
                pos.status = "failed"
                self.db.update_position(pos)
                continue
            pos.status = "unknown"
            self.db.update_position(pos)
            self._halt(f"entry #{pos.id} {pos.symbol} (client order id {pos.client_order_id}) was never "
                       f"confirmed - check the exchange; /forget {pos.id} if nothing was bought")
        if getattr(self.broker, "native_stop_loss", False):
            now = self.market.now_ms()
            for pos in self.db.open_positions(self.mode):
                if not pos.sl_order_id and not pos.closing_reason:
                    self._protect(pos, now)

    def manage_positions(self, now_ms: int) -> None:
        positions = self.db.open_positions(self.mode)
        if not positions:
            return
        issues = []
        try:
            issues = self.broker.sync(positions, now_ms)
        except Exception as exc:
            self._notify_error(f"exchange sync failed: {exc}", key="sync")
        for pos in positions:
            self.db.update_position(pos)
            if pos.status == "closed":
                self.notify(fmt.format_exit(pos, self.cfg.exchange.quote))
        for issue in issues:
            p = issue.position
            if issue.kind == "unprotected":
                self._unprotected(p, issue.message, now_ms)
            elif issue.kind == "mismatch":
                self._halt(f"#{p.id} {p.symbol}: {issue.message}")
            else:
                self._notify_error(f"#{p.id} {p.symbol}: {issue.message}", key=f"sync:{p.id}")
        for pos in positions:
            if pos.status != "open":
                continue
            try:
                if pos.closing_reason:  # an earlier exit didn't complete - finish it
                    self._close(pos, self._price(pos), pos.closing_reason, now_ms)
                else:
                    self._check_position(pos, now_ms)
            except Exception as exc:
                self._notify_error(f"managing #{pos.id} {pos.symbol} failed: {exc}", key=f"manage:{pos.id}")

    def _check_position(self, pos: Position, now_ms: int) -> None:
        """Exit rules - identical for paper and live:

        * the protective stop is a stop-market order on the exchange: any traded price
          through it triggers, filling at the stop (or at a worse gap open);
        * take-profit, breakeven stop and time stop are bot-managed market exits at the
          price available NOW - an old wick through the target is not a fill;
        * breakeven activates after a bar trades +1R and applies from the next bar.
        """
        bar_ms = self.market.price_bar_ms
        long = pos.side == "long"
        bars = self.market.fetch_price_bars(pos.symbol, pos.last_checked_ms)
        if len(bars):
            opens = index_ms(bars.index)
            bars = bars[(opens >= pos.last_checked_ms) & (opens + bar_ms <= now_ms)]  # completed, unseen bars
        be_r = self.cfg.risk.breakeven_at_r
        reason, price = None, None
        for o, h, l in zip(bars["open"].to_numpy(), bars["high"].to_numpy(), bars["low"].to_numpy()):
            if (l <= pos.stop_loss) if long else (h >= pos.stop_loss):
                if pos.breakeven_moved:
                    reason = "breakeven_stop"  # bot-managed: sells at the current price below
                else:
                    gapped = o < pos.stop_loss if long else o > pos.stop_loss
                    reason, price = "stop_loss", (o if gapped else pos.stop_loss)
                break
            if be_r > 0 and not pos.breakeven_moved and pos.r_at(h if long else l) >= be_r:
                self._move_to_breakeven(pos)
        if len(bars):
            pos.last_checked_ms = int(index_ms(bars.index)[-1]) + bar_ms
        if reason is None:
            current = self.market.fetch_last_price(pos.symbol)
            if (current <= pos.stop_loss) if long else (current >= pos.stop_loss):
                reason = "breakeven_stop" if pos.breakeven_moved else "stop_loss"
            elif (current >= pos.take_profit) if long else (current <= pos.take_profit):
                reason = "take_profit"
            elif now_ms >= pos.max_hold_until:
                reason = "time_stop"
            elif be_r > 0 and not pos.breakeven_moved and pos.r_at(current) >= be_r:
                self._move_to_breakeven(pos)
                # the new stop applies from here on: don't judge it against the part of
                # the current bar that happened before (the live price covers the rest)
                pos.last_checked_ms = max(pos.last_checked_ms, (now_ms // bar_ms + 1) * bar_ms)
        elif price is None:
            current = self.market.fetch_last_price(pos.symbol)
        if reason is not None:
            self._close(pos, price if price is not None else current, reason, now_ms)
        else:
            self.db.update_position(pos)

    def _move_to_breakeven(self, pos: Position) -> None:
        self.broker.move_stop(pos, pos.entry_price)
        pos.breakeven_moved = True
        self.db.update_position(pos)
        self.notify(fmt.format_stop_move(pos))

    def _close(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        try:
            self.broker.close_position(pos, price, reason, now_ms)
        except Exception as exc:
            pos.closing_reason = pos.closing_reason or reason
            self.db.update_position(pos)
            self._notify_error(f"Closing #{pos.id} {pos.symbol} ({reason}) is not complete: {exc}. "
                               f"Retrying every poll.", key=f"close:{pos.id}")
            return pos
        self.db.update_position(pos)
        self.notify(fmt.format_exit(pos, self.cfg.exchange.quote))
        return pos

    # ------------------------------------------------------------- periodic
    def _maybe_daily_summary(self, now_ms: int, equity: float) -> None:
        ts = pd.Timestamp(now_ms, unit="ms", tz="UTC")
        day = ts.date().isoformat()
        if ts.hour != self.cfg.telegram.daily_summary_hour_utc or self._get("last_summary") == day:
            return
        self._set("last_summary", day)
        closed = self.db.closed_positions(self.mode, now_ms - 86_400_000)
        open_n = len(self.db.open_positions(self.mode))
        self.notify(
            f"📊 <b>Daily summary</b> ({self.mode})\nEquity: {equity:,.2f} {self.cfg.exchange.quote} · "
            f"open positions: {open_n}\n" + fmt.format_performance(closed, self.cfg.exchange.quote, "Last 24h")
        )

    def _maybe_learn(self, now_ms: int, force: bool = False) -> bool:
        if self.learner is None or (self._learn_thread and self._learn_thread.is_alive()):
            return False
        last = self.db.kv_get("last_learn_ms", 0)
        if not force and now_ms - last < self.cfg.learning.retrain_every_hours * 3_600_000:
            return False
        self.db.kv_set("last_learn_ms", now_ms)

        def work():
            try:
                selection, model, summary = self.learner(self.model)
                self.set_brain(selection, model)
                self.notify("🧠 <b>Self-learning cycle complete</b>\n" + fmt.esc(summary))
            except Exception as exc:
                log.exception("learning cycle failed")
                self._notify_error(f"learning cycle failed: {exc}")

        self._learn_thread = threading.Thread(target=work, name="learning", daemon=True)
        self._learn_thread.start()
        return True

    # ------------------------------------------------------------- commands
    def handle_command(self, cmd: str, args: list[str]) -> str:
        with self._lock:
            quote = self.cfg.exchange.quote
            if cmd in ("start", "help"):
                return HELP
            if cmd == "status":
                equity, _ = self.equity()
                combos = ", ".join(c.key for c in (self.selection.selected if self.selection else [])) or "none"
                state = "halted" if self._get("halted") else "paused" if self._get("paused") else "running"
                return (f"<b>Status</b>: {state} · {self.mode} mode\nEquity: {equity:,.2f} {quote}\n"
                        f"Open positions: {len(self.db.open_positions(self.mode))}/{self.cfg.risk.max_open_positions}\n"
                        f"Strategies: {fmt.esc(combos)}\nML filter: {'active' if self.ml_active else 'not active'}")
            if cmd == "positions":
                _, prices = self.equity()
                return fmt.format_positions(self.db.open_positions(self.mode), prices, quote)
            if cmd == "signals":
                sigs = self.db.recent_signals(8)
                if not sigs:
                    return "No signals yet."
                return "<b>Latest signals</b>\n" + "\n".join(
                    f"{fmt.fmt_time(s.created_at)} {fmt.esc(s.symbol)} {s.side} {s.timeframe} {s.strategy}: "
                    f"{s.status}{' – ' + fmt.esc(s.note) if s.note else ''}" for s in sigs)
            if cmd in ("performance", "stats"):
                closed = self.db.closed_positions(self.mode)
                week = [p for p in closed if (p.closed_at or 0) >= self.market.now_ms() - 7 * 86_400_000]
                return fmt.format_performance(closed, quote, "All time") + "\n\n" + \
                    fmt.format_performance(week, quote, "Last 7 days")
            if cmd == "pause":
                self._set("paused", True)
                return "⏸ Paused. No new trades; open positions are still managed."
            if cmd == "resume":
                equity, _ = self.equity()
                self._set("paused", False)
                self._set("halted", None)
                self._set("peak_equity", equity)
                return "▶️ Resumed."
            if cmd == "close":
                if not args or not args[0].lstrip("#").isdigit():
                    return "Usage: /close &lt;id&gt;"
                pos = self.db.get_position(int(args[0].lstrip("#")))
                if not pos or pos.status != "open" or pos.mode != self.mode:
                    return "No such open position."
                self._close(pos, self.market.fetch_last_price(pos.symbol), "manual", self.market.now_ms())
                return ""
            if cmd == "closeall":
                self._set("paused", True)
                for pos in self.db.open_positions(self.mode):
                    self._close(pos, self.market.fetch_last_price(pos.symbol), "kill_switch", self.market.now_ms())
                return "🛑 All positions closed and trading paused. /resume to continue."
            if cmd == "forget":
                if not args or not args[0].lstrip("#").isdigit():
                    return "Usage: /forget &lt;id&gt;"
                pos = self.db.get_position(int(args[0].lstrip("#")))
                if not pos or pos.mode != self.mode or pos.status == "closed":
                    return "No such position."
                price = self._price(pos) if pos.status == "open" else pos.entry_price
                finalize_close(pos, price, 0.0, "forgotten", self.market.now_ms())
                self.db.update_position(pos)
                return (f"#{pos.id} {fmt.esc(pos.symbol)} marked closed without trading "
                        f"(P&amp;L estimated at {fmt.fmt_price(price)}). Make sure the exchange account matches.")
            if cmd == "learn":
                started = self._maybe_learn(self.market.now_ms(), force=True)
                return "🧠 Learning cycle started - I'll report back when done." if started \
                    else "A learning cycle is already running (or learning is disabled)."
            return f"Unknown command /{fmt.esc(cmd)}. Send /help."
