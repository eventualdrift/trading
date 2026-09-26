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
from .backtest.engine import Costs
from .core import CoreSleeve
from .dashboard import serve_dashboard, strip_html, write_dashboard
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
        self._last_dashboard = 0
        self._dashboard_server = None
        self._brain_stamp = None
        self._last_brain_check = 0
        if cfg.learning.follow_state_dir:
            from .learning import brain_stamp

            self._brain_stamp = brain_stamp(cfg)  # the files the caller loaded the brain from
        self._last_orphan_sweep = 0
        self._ref_price: dict[str, tuple[float, int]] = {}  # last completed 1m close + its time (guard)
        self._pending_extreme: dict[str, int] = {}
        self._stop = threading.Event()
        if hasattr(broker, "persist"):  # live broker saves order ids BEFORE each order is sent
            broker.persist = self.db.update_position
        self.core = (CoreSleeve(db, cfg.core, Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate), self.mode, market)
                     if cfg.core.fraction > 0 and self.mode == "paper" else None)

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

    def _now_ms(self) -> int:
        try:
            return int(self.market.now_ms())
        except Exception:
            return int(time.time() * 1000)

    def notify(self, text: str, event: bool = True) -> None:
        if self.cfg.name:
            text = f"[{fmt.esc(self.cfg.name)}] {text}"
        try:
            if event:  # the dashboard's alert list
                self.db.log_event(self._now_ms(), self.mode, strip_html(text)[:2000])
        except Exception as exc:
            log.warning("event log failed: %s", exc)
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
            try:
                s = self.sleeves(now_ms)
                self.db.record_snapshot(now_ms, self.mode, s["total"], s["core"], s["satellite"], s["btc_price"])
            except Exception as exc:
                log.warning("snapshot failed: %s", exc)

    # ------------------------------------------------------------ core sleeve
    def sleeves(self, now_ms: int) -> dict:
        """Equity by sleeve. Without a core sleeve everything is 'satellite'."""
        sat, _ = self.equity()
        btc = f"BTC/{self.cfg.exchange.quote}"
        syms = set(self.cfg.core.symbols if self.core is not None else []) | {btc}
        prices = {}
        for s in syms:
            try:
                prices[s] = self.market.fetch_last_price(s)
            except Exception:
                pass
        core = self.core.equity(prices) if self.core is not None and self.core.initialized else 0.0
        return {"total": sat + core, "core": core, "satellite": sat, "btc_price": prices.get(btc), "prices": prices}

    def _shift_breakers(self, delta: float) -> None:
        """Capital moved in/out of the satellite is not a gain or loss for its breakers."""
        for key in ("peak_equity", "day_start_equity"):
            v = self._get(key)
            if v is not None:
                self._set(key, float(v) + delta)

    def _maybe_core_day(self, now_ms: int) -> None:
        delay = int(self.cfg.candle_close_delay_seconds * 1000)
        day = last_closed_open_ms(now_ms - delay, "1d")
        if day <= self._get("core_last_day", 0):
            return
        self._set("core_last_day", day)
        c = self.cfg.core
        prices = {}
        for s in c.symbols:
            px = self._guarded_price(s, now_ms)
            if px is not None:
                prices[s] = px
        quote = self.cfg.exchange.quote
        if not self.core.initialized:
            sat, _ = self.equity()
            amount = sat * c.fraction
            self.broker.transfer(-amount)
            self.core.initialize(amount)
            self._shift_breakers(-amount)
            self._set("sleeves_last_ms", now_ms)
            self.notify(f"🏛 <b>Core sleeve started</b>: {amount:,.2f} {quote} ({c.fraction:.0%}) moved into the "
                        f"BTC/ETH trend allocation; {sat - amount:,.2f} {quote} stays with the signal strategies.")
        elif c.rebalance_sleeves_days > 0 and now_ms - self._get("sleeves_last_ms", 0) >= c.rebalance_sleeves_days * 86_400_000:
            self._rebalance_sleeves(now_ms, prices)
        trades = self.core.rebalance(now_ms, prices)
        if trades:
            self.notify(fmt.format_core_rebalance(trades, self.core.weights, self.core.equity(prices), quote))

    def _rebalance_sleeves(self, now_ms: int, prices: dict[str, float]) -> None:
        c, quote = self.cfg.core, self.cfg.exchange.quote
        sat, _ = self.equity()
        core = self.core.equity(prices)
        delta = (sat + core) * c.fraction - core  # > 0: the core is below its share
        self._set("sleeves_last_ms", now_ms)
        if abs(delta) < max(c.min_trade_usd, 0.01 * (sat + core)):
            return
        if delta > 0:
            open_notional = sum(p.notional for p in self.db.open_positions(self.mode))
            moved = max(min(delta, sat - open_notional), 0.0)  # never leave the satellite over-exposed
            if moved <= 0:
                return
            self.broker.transfer(-moved)
            self.core.deposit(moved)
            self._shift_breakers(-moved)
            text = f"{moved:,.2f} {quote} satellite → core"
        else:
            moved, _ = self.core.withdraw(-delta, prices, now_ms)
            self.broker.transfer(moved)
            self._shift_breakers(moved)
            text = f"{moved:,.2f} {quote} core → satellite"
        self.notify(f"⚖️ Sleeves reset to {c.fraction:.0%} core: moved {text}.")

    def _entry_block(self, sig: Signal, open_positions: list[Position], equity: float) -> str | None:
        if self._get("paused"):
            return "paused by user"
        if self._get("halted"):
            return f"halted ({self._get('halted')})"
        if self.risk.daily_limit_hit(equity, self._get("day_start_equity", equity)):
            return "daily loss limit reached"
        burst = self.vol_burst(sig.created_at)
        if burst is not None:
            return burst
        return self.risk.entry_block_reason(sig, open_positions)

    def vol_burst(self, now_ms: int) -> str | None:
        """Volatility circuit breaker: no new entries while BTC's hourly volatility is a
        multiple of its recent normal (off unless guards.vol_breaker is set)."""
        g = self.cfg.guards
        if not g.vol_breaker:
            return None
        ctx = self.scanner.context(now_ms)
        ratio = ctx.vol_ratio_now() if ctx is not None else None
        if ratio is not None and ratio > g.vol_breaker_ratio:
            return f"BTC volatility burst ({ratio:.1f}x normal > {g.vol_breaker_ratio:g}x)"
        return None

    # ------------------------------------------------------------ main loop
    def run_forever(self) -> None:
        tfs = ", ".join(self.active_timeframes()) or "none"
        combos = ", ".join(c.key for c in (self.selection.selected if self.selection else [])) or "none"
        self.notify(
            f"🤖 <b>tradebot started</b> ({self.mode} mode)\nStrategies: {fmt.esc(combos)}\nTimeframes: {tfs}\n"
            f"ML filter: {'active' if self.ml_active else 'not active'}\nSend /help for commands."
        )
        listener = getattr(self.notifier, "start_listener", None)
        if callable(listener) and self.cfg.telegram.commands:
            listener(self.handle_command)
        d = self.cfg.dashboard
        if d.serve:
            try:
                self._dashboard_server = serve_dashboard(self.db, self.cfg, d.port, background=True)
                log.info("dashboard at http://127.0.0.1:%s", d.port)
            except OSError as exc:
                self._notify_error(f"dashboard could not use port {d.port}: {exc}", key="dashboard")
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
        if self._dashboard_server is not None:
            self._dashboard_server.shutdown()

    def tick(self, now_ms: int) -> None:
        with self._lock:
            self.manage_positions(now_ms)
            equity, prices = self.equity()
            self._update_breakers(now_ms, equity)
            self._maybe_write_dashboard(now_ms, prices)
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
            if self.core is not None:
                try:
                    self._maybe_core_day(now_ms)
                except Exception as exc:
                    self._notify_error(f"core sleeve: {exc}", key="core")
            self._maybe_daily_summary(now_ms, equity)
        if self.cfg.learning.follow_state_dir:
            self._maybe_follow_brain(now_ms)
        else:
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
        # resting limit entries count toward the position limits and exposure
        open_positions = self.db.open_positions(self.mode) + self.db.positions_with_status(self.mode, ("working",))
        limit_entry = self.cfg.costs.entry_order == "limit"
        block = self._entry_block(sig, open_positions, equity)
        price, size = None, None
        if block is None:
            price = self._guarded_price(sig.symbol, now_ms, ref=sig.entry)
            if price is None:
                block = "price not confirmed (stale or extreme) - bad-data guard"
        if block is None:
            limit = sig.chase_limit(self.cfg.risk.max_chase_r)
            beyond = price >= limit if sig.side == "long" else price <= limit
            invalid = (price <= sig.stop_loss or price >= sig.take_profit) if sig.side == "long" \
                else (price >= sig.stop_loss or price <= sig.take_profit)
            if beyond or invalid:
                block = f"price {fmt.fmt_price(price)} already outside the entry zone"
        if block is None:
            size = self.risk.size(
                equity, sig.entry if limit_entry else price, sig.stop_loss,
                open_notional=sum(p.notional for p in open_positions),
                available_cash=self.broker.available_cash(),
                limits=self.broker.limits(sig.symbol),
                to_precision=self.broker.to_precision(sig.symbol),
                risk_multiplier=sig.risk_multiplier,
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
        if limit_entry:  # paper only (config validation): rest a buy limit at the signal close
            bar = self.market.price_bar_ms
            pos.status, pos.limit_until = "working", sig.valid_until
            pos.last_checked_ms = (now_ms // bar + 1) * bar
            self.db.insert_position(pos)
            self.notify(fmt.format_signal(sig, pos, equity, self.mode, self.cfg.exchange.quote,
                                          self.cfg.risk.max_chase_r, self.ml_active))
            return pos
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
            # no new trades until a human has looked: protection failing once usually means
            # it will fail for the next trade too
            self._halt(f"#{pos.id} {pos.symbol} had no exchange stop-loss ({why})")
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
    def unresolved_entries(self) -> list[Position]:
        return self.db.positions_with_status(self.mode, ("pending", "unknown"))

    def check_unresolved(self) -> None:
        """At start-up: entries the bot sent but never confirmed become 'unknown' (they
        are reconciled every poll), stray bot orders are cancelled, and every live
        position gets its exchange stop."""
        for pos in self.unresolved_entries():
            if self.mode == "paper":
                pos.status = "failed"
                self.db.update_position(pos)
                continue
            pos.status = "unknown"
            self.db.update_position(pos)
            self._halt(f"entry #{pos.id} {pos.symbol} (client order id {pos.client_order_id}) was never confirmed")
        self._reconcile_unknown(self.market.now_ms())
        self._sweep_orphans(self.market.now_ms(), force=True)
        if getattr(self.broker, "native_stop_loss", False):
            now = self.market.now_ms()
            for pos in self.db.open_positions(self.mode):
                if not pos.sl_order_id and not pos.closing_reason:
                    self._protect(pos, now)

    def _reconcile_unknown(self, now_ms: int) -> None:
        reconcile = getattr(self.broker, "reconcile_entry", None)
        if reconcile is None:
            return
        for pos in self.db.positions_with_status(self.mode, ("unknown",)):
            try:
                status = reconcile(pos, now_ms)
            except Exception as exc:
                self._notify_error(f"entry #{pos.id} {pos.symbol} still unresolved: {exc}", key=f"unknown:{pos.id}")
                continue
            self.db.update_position(pos)
            if status == "open":
                bar = self.market.price_bar_ms
                pos.last_checked_ms = (now_ms // bar + 1) * bar
                self.db.update_position(pos)
                self.notify(f"✅ Entry #{pos.id} {fmt.esc(pos.symbol)} confirmed on the exchange "
                            f"({pos.amount:g} @ {fmt.fmt_price(pos.entry_price)}); protecting and managing it.")
                self._protect(pos, now_ms)
            else:
                self.notify(f"✅ Entry #{pos.id} {fmt.esc(pos.symbol)} confirmed as never filled.")

    def _sweep_orphans(self, now_ms: int, force: bool = False) -> None:
        """Cancel open orders the bot created that no position owns (e.g. a stop whose
        placement timed out). Runs at start-up and hourly."""
        sweep = getattr(self.broker, "cancel_orphans", None)
        if sweep is None or (not force and now_ms - self._last_orphan_sweep < 3_600_000):
            return
        self._last_orphan_sweep = now_ms
        recent = self.db.positions_since(self.mode, now_ms - 14 * 86_400_000)
        active = [p for p in recent if p.status in ("open", "pending", "unknown")]
        known = {str(v) for p in active for v in (p.entry_order_id, p.sl_order_id, p.exit_order_id,
                                                   p.client_order_id, p.sl_client_id, p.exit_client_id) if v}
        try:
            cancelled = sweep({p.symbol for p in recent}, known)
        except Exception as exc:
            self._notify_error(f"stray-order check failed: {exc}", key="orphans")
            return
        if cancelled:
            self.notify("🧹 Cancelled stray bot orders no position owns:\n" + fmt.esc("\n".join(cancelled)))

    def _work_limit_orders(self, now_ms: int) -> None:
        """Paper limit entries: fill when price trades THROUGH the limit, else expire."""
        bar_ms = self.market.price_bar_ms
        for pos in self.db.positions_with_status(self.mode, ("working",)):
            long = pos.side == "long"
            filled_bar = None
            bars = self.market.fetch_price_bars(pos.symbol, pos.last_checked_ms)
            if len(bars):
                opens = index_ms(bars.index)
                bars = bars[(opens >= pos.last_checked_ms) & (opens + bar_ms <= now_ms)
                            & (opens < pos.limit_until)]
                for ts, h, l in zip(index_ms(bars.index), bars["high"].to_numpy(), bars["low"].to_numpy()):
                    if (l < pos.entry_price) if long else (h > pos.entry_price):
                        filled_bar = int(ts)
                        break
                if filled_bar is None and len(bars):
                    pos.last_checked_ms = int(index_ms(bars.index)[-1]) + bar_ms
            if filled_bar is None and now_ms < pos.limit_until:
                current = self._guarded_price(pos.symbol, now_ms, ref=pos.entry_price)
                if current is not None and ((current < pos.entry_price) if long else (current > pos.entry_price)):
                    filled_bar = (now_ms // bar_ms) * bar_ms
            if filled_bar is not None:
                self.broker.fill_limit(pos, now_ms)
                pos.last_checked_ms = filled_bar  # replay the fill bar: a stop there still counts
                self.db.update_position(pos)
                self.notify(f"✅ Limit filled — {fmt.esc(pos.symbol)} #{pos.id} at "
                            f"<code>{fmt.fmt_price(pos.entry_price)}</code>. Stop-loss "
                            f"<code>{fmt.fmt_price(pos.stop_loss)}</code>, take-profit "
                            f"<code>{fmt.fmt_price(pos.take_profit)}</code>.")
            elif now_ms >= pos.limit_until:
                pos.status, pos.exit_reason = "missed", "limit not filled"
                self.db.update_position(pos)
                self.notify(f"⌛ Limit order expired — {fmt.esc(pos.symbol)} #{pos.id} never traded through "
                            f"<code>{fmt.fmt_price(pos.entry_price)}</code>. No trade.")
            else:
                self.db.update_position(pos)

    def manage_positions(self, now_ms: int) -> None:
        self._reconcile_unknown(now_ms)
        self._sweep_orphans(now_ms)
        if hasattr(self.broker, "fill_limit"):
            try:
                self._work_limit_orders(now_ms)
            except Exception as exc:
                self._notify_error(f"limit orders: {exc}", key="limits")
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
        be_r = self.cfg.risk.breakeven_at_r or (1.0 if pos.trail_distance else 0.0)
        reason, price = None, None
        # Replay every completed bar since the last check, in order, one page at a time -
        # after downtime the backlog can exceed one page, and the stop must be found
        # before the current price is looked at.
        caught_up = False
        for _ in range(200):
            if reason is not None:
                break
            if pos.last_checked_ms + bar_ms > now_ms:
                caught_up = True
                break
            bars = self.market.fetch_price_bars(pos.symbol, pos.last_checked_ms)
            if len(bars):
                opens = index_ms(bars.index)
                bars = bars[(opens >= pos.last_checked_ms) & (opens + bar_ms <= now_ms)]  # completed, unseen
            if not len(bars):  # nothing newer: history is fully replayed
                caught_up = True
                break
            self._ref_price[pos.symbol] = (float(bars["close"].iloc[-1]), int(index_ms(bars.index)[-1]) + bar_ms)
            for ts, o, h, l in zip(index_ms(bars.index), bars["open"].to_numpy(), bars["high"].to_numpy(),
                                   bars["low"].to_numpy()):
                pos.last_checked_ms = int(ts) + bar_ms
                if (l <= pos.stop_loss) if long else (h >= pos.stop_loss):
                    if pos.breakeven_moved:
                        reason = self._managed_stop_reason(pos)  # bot-managed: sells at the current price
                    else:
                        gapped = o < pos.stop_loss if long else o > pos.stop_loss
                        reason, price = "stop_loss", (o if gapped else pos.stop_loss)
                    break
                if be_r > 0 and not pos.breakeven_moved and pos.r_at(h if long else l) >= be_r:
                    self._move_to_breakeven(pos)
                if pos.breakeven_moved and pos.trail_distance:
                    self._trail(pos, h if long else l)
        if reason is None and not caught_up:
            # a very long backlog: keep replaying next poll before trusting the live price
            log.warning("#%s %s: still replaying price history", pos.id, pos.symbol)
            self.db.update_position(pos)
            return
        if reason is None:
            current = self._guarded_price(pos.symbol, now_ms)
            if current is None:  # stale or unconfirmed extreme price: decide next poll
                self.db.update_position(pos)
                return
            if (current <= pos.stop_loss) if long else (current >= pos.stop_loss):
                reason = self._managed_stop_reason(pos) if pos.breakeven_moved else "stop_loss"
            elif (current >= pos.take_profit) if long else (current <= pos.take_profit):
                reason = "take_profit"
            elif now_ms >= pos.max_hold_until:
                reason = "time_stop"
            elif be_r > 0 and not pos.breakeven_moved and pos.r_at(current) >= be_r:
                self._move_to_breakeven(pos)
                # the new stop applies from here on: don't judge it against the part of
                # the current bar that happened before (the live price covers the rest)
                pos.last_checked_ms = max(pos.last_checked_ms, (now_ms // bar_ms + 1) * bar_ms)
        elif price is None:  # a completed bar hit the bot-managed stop: exit now
            current = self._guarded_price(pos.symbol, now_ms)
            if current is None:
                current = pos.stop_loss  # price unconfirmed: book the stop level itself
        if reason is not None:
            self._close(pos, price if price is not None else current, reason, now_ms)
        else:
            self.db.update_position(pos)

    def _guarded_price(self, symbol: str, now_ms: int, ref: float | None = None) -> float | None:
        """Bad-data guard for the live price: None means "don't act on it this poll".

        * stale: the exchange's ticker is older than guards.max_price_age_seconds;
        * extreme: more than guards.extreme_move_pct away from the last completed 1m close
          (or ``ref``) - acted on only if it is still there on the next poll. In live
          trading the exchange-side stop keeps protecting the position meanwhile.
        """
        g = self.cfg.guards
        fetch_ts = getattr(self.market, "fetch_last_price_ts", None)
        price, ts = fetch_ts(symbol) if callable(fetch_ts) else (self.market.fetch_last_price(symbol), None)
        if ts is not None and g.max_price_age_seconds > 0 and now_ms - ts > g.max_price_age_seconds * 1000:
            self._notify_error(f"{symbol}: the exchange price is {(now_ms - ts) / 1000:.0f}s old - "
                               f"not acting on it until it updates", key=f"stale:{symbol}")
            return None
        if ref is None:  # only trust a recent close (a remembered one goes stale after the trade)
            close, at = self._ref_price.get(symbol, (None, 0))
            ref = close if now_ms - at <= 15 * 60_000 else None
        if ref and g.extreme_move_pct > 0 and abs(price / ref - 1) * 100 > g.extreme_move_pct:
            direction = 1 if price > ref else -1
            if self._pending_extreme.get(symbol) != direction:
                self._pending_extreme[symbol] = direction
                log.warning("%s: price %s is %.1f%% from the last close %s - confirming on the next poll",
                            symbol, price, (price / ref - 1) * 100, ref)
                return None
            log.warning("%s: extreme price %s confirmed", symbol, price)
        self._pending_extreme.pop(symbol, None)
        return price

    def _move_to_breakeven(self, pos: Position) -> None:
        self.broker.move_stop(pos, pos.entry_price)
        pos.breakeven_moved = True
        pos.trail_notified = pos.entry_price
        self.db.update_position(pos)
        self.notify(fmt.format_stop_move(pos))

    def _trail(self, pos: Position, extreme: float) -> None:
        """Ratchet the (bot-managed) stop behind the best price; never loosen it."""
        if pos.side == "long":
            new = max(pos.stop_loss, extreme - pos.trail_distance)
        else:
            new = min(pos.stop_loss, extreme + pos.trail_distance)
        if new == pos.stop_loss:
            return
        self.broker.move_stop(pos, new)
        per_unit = abs(pos.entry_price - pos.initial_stop)
        # tell signal followers when it has moved by at least half the original risk
        if per_unit > 0 and abs(new - (pos.trail_notified or pos.entry_price)) >= 0.5 * per_unit:
            pos.trail_notified = new
            self.notify(fmt.format_trail_move(pos))
        self.db.update_position(pos)

    @staticmethod
    def _managed_stop_reason(pos: Position) -> str:
        beyond = pos.stop_loss > pos.entry_price if pos.side == "long" else pos.stop_loss < pos.entry_price
        return "trailing_stop" if beyond else "breakeven_stop"

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
    def _maybe_write_dashboard(self, now_ms: int, prices: dict[str, float]) -> None:
        d = self.cfg.dashboard
        if not d.enabled or now_ms - self._last_dashboard < d.every_minutes * 60_000:
            return
        self._last_dashboard = now_ms
        try:
            self._set("last_prices", prices)  # lets `tradebot dashboard` show open P&L without the exchange
            write_dashboard(self.db, self.cfg, prices=prices, now_ms=now_ms)
        except Exception as exc:
            log.warning("dashboard update failed: %s", exc)

    def _maybe_daily_summary(self, now_ms: int, equity: float) -> None:
        ts = pd.Timestamp(now_ms, unit="ms", tz="UTC")
        day = ts.date().isoformat()
        if ts.hour != self.cfg.telegram.daily_summary_hour_utc or self._get("last_summary") == day:
            return
        self._set("last_summary", day)
        closed = self.db.closed_positions(self.mode, now_ms - 86_400_000)
        open_n = len(self.db.open_positions(self.mode))
        q = self.cfg.exchange.quote
        line = f"Equity: {equity:,.2f} {q}"
        if self.core is not None and self.core.initialized:
            s = self.sleeves(now_ms)
            line = (f"Total equity: {s['total']:,.2f} {q} (core {s['core']:,.2f} · "
                    f"satellite {s['satellite']:,.2f})")
        self.notify(
            f"📊 <b>Daily summary</b> ({self.mode})\n{line} · open positions: {open_n}\n"
            + fmt.format_performance(closed, q, "Last 24h"),
            event=False,  # routine, and the dashboard shows the same numbers
        )

    def _maybe_follow_brain(self, now_ms: int) -> None:
        """Side-by-side instance: pick up the leader's selection/model whenever it retrains."""
        if now_ms - self._last_brain_check < 5 * 60_000:
            return
        self._last_brain_check = now_ms
        from .learning import brain_stamp, load_brain

        try:
            stamp = brain_stamp(self.cfg)
            if stamp == self._brain_stamp:
                return
            selection, model = load_brain(self.cfg)
            self._brain_stamp = stamp
            self.set_brain(selection, model)
            combos = ", ".join(c.key for c in (selection.selected if selection else [])) or "none"
            self.notify(f"🧠 Strategies reloaded from {fmt.esc(self.cfg.learning.follow_state_dir)}: "
                        f"{fmt.esc(combos)}")
        except Exception as exc:
            self._notify_error(f"could not reload strategies from {self.cfg.learning.follow_state_dir}: {exc}",
                               key="brain")

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
                if self.core is not None and self.core.initialized:
                    s = self.sleeves(self.market.now_ms())
                    w = ", ".join(f"{k.split('/')[0]} {v:.0%}" for k, v in self.core.weights.items()) or "no weights yet"
                    money = (f"Total equity: {s['total']:,.2f} {quote}\n"
                             f"• Core: {s['core']:,.2f} {quote} (P&amp;L {s['core'] - self.core.contributed:+,.2f}; {w})\n"
                             f"• Satellite: {equity:,.2f} {quote}")
                else:
                    money = f"Equity: {equity:,.2f} {quote}"
                return (f"<b>Status</b>: {state} · {self.mode} mode\n{money}\n"
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
                pending = self.unresolved_entries()
                if pending:
                    ids = ", ".join(f"#{p.id} {fmt.esc(p.symbol)}" for p in pending)
                    return (f"Can't resume: entry outcome still unknown for {ids}. The bot keeps checking; "
                            f"if you've confirmed on the exchange that nothing was bought, send /forget &lt;id&gt;.")
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
                if not pos or pos.mode != self.mode or pos.status in ("closed", "failed"):
                    return "No such position."
                if pos.status in ("pending", "unknown"):
                    pos.status, pos.exit_reason = "failed", "forgotten"
                    self.db.update_position(pos)
                    return f"Entry #{pos.id} {fmt.esc(pos.symbol)} marked as never filled."
                price = self._price(pos)
                finalize_close(pos, price, 0.0, "forgotten", self.market.now_ms())
                self.db.update_position(pos)
                return (f"#{pos.id} {fmt.esc(pos.symbol)} marked closed without trading "
                        f"(P&amp;L estimated at {fmt.fmt_price(price)}). Make sure the exchange account matches.")
            if cmd == "learn" and self.cfg.learning.follow_state_dir:
                return (f"This instance uses the strategies of {fmt.esc(self.cfg.learning.follow_state_dir)} - "
                        f"run /learn there.")
            if cmd == "learn":
                started = self._maybe_learn(self.market.now_ms(), force=True)
                return "🧠 Learning cycle started - I'll report back when done." if started \
                    else "A learning cycle is already running (or learning is disabled)."
            return f"Unknown command /{fmt.esc(cmd)}. Send /help."
