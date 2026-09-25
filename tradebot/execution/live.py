"""Live spot trading through ccxt - Binance spot only in this version.

Why only Binance: safe live execution depends on exchange details that differ per
venue and must be verified on a testnet. On Binance spot a stop-loss is an
ordinary order that keeps its id from placement through trigger to fill, reports
its executed quantity, and locks the coins it would sell. Orders can be looked
up by our client order id, and the exchange rejects any request older than
`recvWindow` - so once that window has passed, an id Binance doesn't know was
never placed. Other exchanges (OKX trigger orders, Bybit account modes, ...)
need their own adapter and testnet validation before they are allowed here.

Rules this broker follows:
  * every order gets a client order id that is persisted BEFORE it is sent;
  * an order whose outcome is unclear is resolved by looking up that id - never
    by attributing balance changes to it;
  * fills come from the order's executed quantity ("closed" alone proves nothing),
    recorded per order id so re-reading an order can't double count;
  * an earlier exit order is driven to a final state before any new sell;
  * a stop counts as protection only after it's fetched back and checked:
    open, our symbol, side, trigger and full quantity;
  * the breakeven stop is enforced by the bot; the exchange stop stays at the
    initial level (no cancel-then-replace window).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import ccxt

from ..data.exchange import ExchangeClient, with_retries
from ..models import Position, new_client_id
from .base import Broker, ExecutionError, NotFilled, ProtectionError, SyncIssue, finalize_close

log = logging.getLogger(__name__)

FINAL = ("closed", "canceled", "expired", "rejected")
BOT_PREFIX = "tb"


@dataclass(frozen=True)
class Venue:
    """Exchange-specific parameters for addressing orders."""

    order_fetch: dict = field(default_factory=dict)
    stop_create: dict = field(default_factory=dict)
    stop_fetch: dict = field(default_factory=dict)
    stop_cancel: dict = field(default_factory=dict)


# Only venues whose full order lifecycle has been worked through may trade live.
VENUES: dict[str, Venue] = {
    "binance": Venue(),  # stop-losses are ordinary spot orders (STOP_LOSS / STOP_LOSS_LIMIT)
}


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, client: ExchangeClient, quote: str, native_stop_loss: bool = True,
                 fill_timeout_s: float = 30.0, sleep=time.sleep, clock=None, persist=None):
        venue = VENUES.get(client.id)
        if venue is None:
            raise ValueError(
                f"Live trading is not supported on {client.id!r}: its order handling has not been verified. "
                f"Supported: {', '.join(sorted(VENUES))}. Signals and paper trading work on any exchange."
            )
        self.client = client
        self.ex = client.ex
        self.venue = venue
        self.quote = quote
        self.native_stop_loss = native_stop_loss
        self.fill_timeout_s = fill_timeout_s
        self.sleep = sleep
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.persist = persist or (lambda pos: None)
        self.recv_window_ms = int((getattr(self.ex, "options", None) or {}).get("recvWindow", 10_000))

    # ------------------------------------------------------------- helpers
    def _base(self, symbol: str) -> str:
        return self.client.market(symbol)["base"]

    def _balance(self) -> dict:
        return with_retries(self.ex.fetch_balance)

    def _held(self, base: str, kind: str = "total") -> float:
        return float((self._balance().get(kind) or {}).get(base) or 0.0)

    def _fetch(self, order_id: str, symbol: str, stop: bool = False) -> dict:
        params = dict(self.venue.stop_fetch if stop else self.venue.order_fetch)
        return with_retries(self.ex.fetch_order, order_id, symbol, params)

    def _lookup(self, symbol: str, client_id: str, sent_ms: int, stop: bool = False) -> dict | None:
        """Find an order by our client id. None means it was definitely never placed
        (checked after the exchange's recvWindow expired). Other errors propagate: unknown."""
        wait = sent_ms + self.recv_window_ms + 2_000 - self.clock()
        if wait > 0:
            self.sleep(wait / 1000.0)
        params = dict(self.venue.stop_fetch if stop else self.venue.order_fetch)
        params["clientOrderId"] = client_id
        try:
            return with_retries(self.ex.fetch_order, None, symbol, params)
        except ccxt.OrderNotFound:
            return None

    def _cancel(self, order_id: str, symbol: str, stop: bool = False) -> None:
        params = dict(self.venue.stop_cancel) if stop else {}
        try:
            self.ex.cancel_order(order_id, symbol, params)
        except ccxt.OrderNotFound:
            pass  # already final - the fetch that follows tells which
        except ccxt.NetworkError as exc:
            log.warning("cancel %s on %s failed (%s); state is re-read next", order_id, symbol, exc)

    def _settle(self, order: dict, symbol: str) -> dict:
        """Drive an order to a final state (cancelling any unfilled remainder)."""
        for _ in range(max(int(self.fill_timeout_s), 1)):
            if order.get("status") in FINAL:
                break
            self.sleep(1.0)
            order = self._fetch(order["id"], symbol)
        if order.get("status") not in FINAL:
            self._cancel(order["id"], symbol)
            order = self._fetch(order["id"], symbol)
        if order.get("status") not in FINAL:
            raise ExecutionError(f"order {order['id']} on {symbol} still {order.get('status')} after cancelling")
        if order.get("filled") is None:
            raise ExecutionError(f"order {order['id']} on {symbol} reports no executed quantity")
        return order

    def _fees(self, order: dict, base: str, avg: float) -> tuple[float, float]:
        """-> (fee taken in the base coin, all fees valued in quote)."""
        fees = list(order.get("fees") or []) or ([order["fee"]] if order.get("fee") else [])
        base_fee = quote_fee = 0.0
        for f in fees:
            if not f or not f.get("cost"):
                continue
            cost, cur = float(f["cost"]), f.get("currency")
            if cur == base:
                base_fee += cost
                quote_fee += cost * avg
            elif cur in (self.quote, None):
                quote_fee += cost
            else:  # e.g. a BNB fee discount
                try:
                    quote_fee += cost * self.client.fetch_last_price(f"{cur}/{self.quote}")
                except Exception:
                    log.warning("could not value a %s %s fee in %s", cost, cur, self.quote)
        return base_fee, quote_fee

    def _apply(self, pos: Position, order: dict, kind: str) -> None:
        """Record an order's (cumulative) fills on the position - idempotent per order id."""
        filled = float(order.get("filled") or 0.0)
        avg = 0.0
        if filled > 0:
            if order.get("average"):
                avg = float(order["average"])
            elif order.get("cost"):
                avg = float(order["cost"]) / filled
            else:
                avg = float(order.get("price") or 0.0)
            if avg <= 0:
                raise ExecutionError(f"order {order.get('id')} filled {filled} but reports no price")
        base_fee, quote_fee = self._fees(order, self._base(pos.symbol), avg)
        fills = dict(pos.fills)
        fills[str(order["id"])] = {"kind": kind, "filled": filled, "avg": avg, "base_fee": base_fee, "fee": quote_fee}
        pos.fills = fills
        entry = [f for f in fills.values() if f["kind"] == "entry"]
        exits = [f for f in fills.values() if f["kind"] in ("exit", "stop")]
        if entry:
            gross = sum(f["filled"] for f in entry)
            if gross > 0:
                pos.entry_price = sum(f["filled"] * f["avg"] for f in entry) / gross
            pos.amount = gross - sum(f["base_fee"] for f in entry)  # coins actually received
        pos.exit_filled = sum(f["filled"] for f in exits)
        pos.exit_value = sum(f["filled"] * f["avg"] for f in exits)
        pos.fees = sum(f["fee"] for f in fills.values())

    def _is_dust(self, symbol: str, qty: float, price: float) -> bool:
        if qty <= 0 or self.client.amount_to_precision(symbol, qty) <= 0:
            return True
        lim = self.client.limits(symbol)
        return qty < (lim.get("min_amount") or 0.0) or qty * price < (lim.get("min_cost") or 0.0)

    # ------------------------------------------------------------ balances
    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        totals = self._balance().get("total") or {}
        value = float(totals.get(self.quote) or 0.0)
        for p in positions:
            held = float(totals.get(self._base(p.symbol)) or 0.0)
            value += min(p.open_amount, held) * prices.get(p.symbol, p.entry_price)  # only coins really held
        return value

    def available_cash(self) -> float | None:
        return self._held(self.quote, "free")

    def limits(self, symbol: str) -> dict:
        return self.client.limits(symbol)

    def to_precision(self, symbol: str):
        return lambda a: self.client.amount_to_precision(symbol, a)

    # --------------------------------------------------------------- entry
    def open_position(self, pos: Position, price: float, now_ms: int) -> Position:
        if pos.side != "long":
            raise NotFilled("live spot trading is long-only")
        if not pos.client_order_id:
            raise ValueError("position needs a client_order_id persisted before the order is sent")
        sym = pos.symbol
        amount = self.client.amount_to_precision(sym, pos.amount)
        sent = self.clock()
        try:
            order = self.ex.create_order(sym, "market", "buy", amount, None, {"clientOrderId": pos.client_order_id})
        except ccxt.NetworkError as exc:  # the order may or may not exist
            log.error("entry for %s: %s - looking it up by client id", sym, exc)
            order = self._lookup(sym, pos.client_order_id, sent)  # raises if still unknown
            if order is None:
                raise NotFilled(f"entry for {sym} was never placed (not known to {self.client.id})") from exc
        except ccxt.BaseError as exc:
            raise NotFilled(f"entry rejected by {self.client.id}: {exc}") from exc
        pos.entry_order_id = str(order["id"])
        self.persist(pos)
        self._finish_entry(pos, self._settle(order, sym), now_ms)
        return pos

    def _finish_entry(self, pos: Position, order: dict, now_ms: int) -> None:
        self._apply(pos, order, "entry")
        if pos.amount <= 0:
            raise NotFilled(f"entry order {order.get('id')} filled nothing ({order.get('status')})")
        pos.opened_at = pos.opened_at or now_ms
        pos.status = "open"

    def reconcile_entry(self, pos: Position, now_ms: int) -> str:
        """Resolve an entry whose outcome was never confirmed -> 'open' | 'failed'.
        Raises if the exchange still can't tell us."""
        sym = pos.symbol
        if pos.entry_order_id:
            order = self._fetch(pos.entry_order_id, sym)
        else:
            order = self._lookup(sym, pos.client_order_id, pos.opened_at)
            if order is None:
                pos.status = "failed"
                return pos.status
            pos.entry_order_id = str(order["id"])
            self.persist(pos)
        try:
            self._finish_entry(pos, self._settle(order, sym), now_ms)
        except NotFilled:
            pos.status = "failed"
        return pos.status

    # ---------------------------------------------------------- protection
    def protect(self, pos: Position, now_ms: int) -> None:
        if not self.native_stop_loss:
            return
        sym = pos.symbol
        qty = self.client.amount_to_precision(sym, pos.open_amount)
        stop = self.client.price_to_precision(sym, pos.initial_stop)
        problems = []
        for otype, limit in (("market", None), ("limit", self.client.price_to_precision(sym, stop * 0.995))):
            pos.sl_client_id, pos.sl_sent_ms = new_client_id("s"), self.clock()
            self.persist(pos)
            params = {"stopLossPrice": stop, "clientOrderId": pos.sl_client_id, **self.venue.stop_create}
            try:
                order = self.ex.create_order(sym, otype, "sell", qty, limit, params)
            except ccxt.NetworkError as exc:
                try:
                    order = self._lookup(sym, pos.sl_client_id, pos.sl_sent_ms, stop=True)
                except ccxt.BaseError as exc2:
                    raise ProtectionError(f"stop order outcome unknown ({exc}; lookup: {exc2})") from exc2
                if order is None:
                    pos.sl_client_id = None
                    problems.append(f"{otype} stop not placed ({exc})")
                    continue
            except ccxt.BaseError as exc:
                pos.sl_client_id = None
                problems.append(f"{otype} stop rejected: {exc}")
                continue
            state, detail = self._verify_stop(order, sym, stop, qty)
            if state == "ok":
                pos.sl_order_id = str(order["id"])
                self.persist(pos)
                log.info("verified %s stop for %s %s @ %s (order %s)", otype, qty, sym, stop, order["id"])
                return
            if state == "executed":  # the exchange sold instead of waiting for the trigger
                self._apply(pos, detail, "stop")
                pos.sl_client_id = None
                if self._is_dust(sym, pos.open_amount, pos.initial_stop):
                    finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, "stop_order_executed", now_ms)
                    return
                raise ProtectionError("the stop order executed immediately for part of the position")
            self._cancel(order["id"], sym, stop=True)
            final = self._fetch(order["id"], sym, stop=True)
            if final.get("status") not in FINAL:
                raise ProtectionError(f"a non-protective order {order['id']} could not be cancelled")
            self._apply(pos, final, "stop")  # in case part of it executed
            pos.sl_client_id = None
            problems.append(f"{otype} stop not protective ({detail})")
        raise ProtectionError("; ".join(problems))

    def _verify_stop(self, order: dict, symbol: str, stop: float, qty: float) -> tuple[str, object]:
        try:
            o = self._fetch(order["id"], symbol, stop=True)
        except ccxt.BaseError as exc:
            raise ProtectionError(f"could not read back stop order {order.get('id')}: {exc}") from exc
        if float(o.get("filled") or 0.0) > 0:
            return "executed", o
        if o.get("status") != "open":
            return "bad", f"status {o.get('status')!r}"
        if o.get("symbol") not in (None, symbol):
            return "bad", f"symbol {o.get('symbol')}"
        if (o.get("side") or "sell") != "sell":
            return "bad", f"side {o.get('side')}"
        trig = next((o.get(k) for k in ("triggerPrice", "stopLossPrice", "stopPrice") if o.get(k)), None)
        if trig is None or abs(float(trig) - stop) > 0.002 * stop:
            return "bad", f"trigger {trig} instead of {stop}"
        remaining = float(o.get("remaining") if o.get("remaining") is not None else (o.get("amount") or 0.0))
        if remaining < qty * 0.999:
            return "bad", f"covers {remaining:g} of {qty:g}"
        return "ok", o

    # ---------------------------------------------------------------- exit
    def _resolve_exit_order(self, pos: Position) -> None:
        """Bring an earlier sell to a final state and record it, before any new sell."""
        sym = pos.symbol
        if pos.exit_order_id:
            order = self._fetch(pos.exit_order_id, sym)
        elif pos.exit_client_id:
            order = self._lookup(sym, pos.exit_client_id, pos.exit_sent_ms)
            if order is None:  # never placed
                pos.exit_client_id = None
                self.persist(pos)
                return
        else:
            return
        pos.exit_order_id = str(order["id"])
        order = self._settle(order, sym)
        self._apply(pos, order, "exit")
        pos.exit_order_id = pos.exit_client_id = None
        self.persist(pos)

    def _release_stop(self, pos: Position) -> None:
        """Cancel our exchange stop (known or only sent) and record whatever it sold."""
        sym = pos.symbol
        if not pos.sl_order_id and pos.sl_client_id:  # sent, outcome never confirmed
            found = self._lookup(sym, pos.sl_client_id, pos.sl_sent_ms, stop=True)
            if found is None:
                pos.sl_client_id = None
                self.persist(pos)
                return
            pos.sl_order_id = str(found["id"])
        if not pos.sl_order_id:
            return
        oid = pos.sl_order_id
        self._cancel(oid, sym, stop=True)
        try:
            final = self._fetch(oid, sym, stop=True)
        except ccxt.BaseError as exc:
            raise ExecutionError(f"could not confirm stop {oid} for {sym} is cancelled ({exc}); not selling") from exc
        if final.get("status") not in FINAL:
            raise ExecutionError(f"stop {oid} for {sym} is still {final.get('status')}; not selling")
        self._apply(pos, final, "stop")
        pos.sl_order_id = pos.sl_client_id = None
        self.persist(pos)

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        sym = pos.symbol
        pos.closing_reason = reason
        self._resolve_exit_order(pos)
        had_stop_fill = any(f["kind"] == "stop" and f["filled"] > 0 for f in pos.fills.values())
        self._release_stop(pos)
        stopped = not had_stop_fill and any(f["kind"] == "stop" and f["filled"] > 0 for f in pos.fills.values())
        if stopped and self._is_dust(sym, pos.open_amount, price):
            reason = "stop_loss"  # the exchange stop got there first and sold everything
        if not self._is_dust(sym, pos.open_amount, price):
            self._sell(pos, price)
        if not self._is_dust(sym, pos.open_amount, price):
            raise ExecutionError(f"{pos.open_amount:g} {self._base(sym)} still held after selling; will retry")
        exit_price = pos.exit_value / pos.exit_filled if pos.exit_filled else price
        return finalize_close(pos, exit_price, 0.0, reason, now_ms)

    def _sell(self, pos: Position, price: float) -> None:
        sym, base = pos.symbol, self._base(pos.symbol)
        held = self._held(base, "free")
        qty = self.client.amount_to_precision(sym, min(pos.open_amount, held))
        if self._is_dust(sym, qty, price):
            raise ExecutionError(
                f"the bot expects {pos.open_amount:g} {base} but only {held:g} is free - sold outside the bot? "
                f"Not selling. Check the account, then /forget {pos.id}"
            )
        pos.exit_client_id, pos.exit_sent_ms = new_client_id("x"), self.clock()
        self.persist(pos)
        try:
            order = self.ex.create_order(sym, "market", "sell", qty, None, {"clientOrderId": pos.exit_client_id})
        except ccxt.NetworkError as exc:
            raise ExecutionError(f"sell of {qty:g} {base} unconfirmed ({exc}); resolving it before any retry") from exc
        except ccxt.BaseError as exc:
            pos.exit_client_id = None
            self.persist(pos)
            raise ExecutionError(f"sell of {qty:g} {base} rejected: {exc}") from exc
        pos.exit_order_id = str(order["id"])
        self.persist(pos)
        self._resolve_exit_order(pos)

    # ---------------------------------------------------------------- sync
    def sync(self, positions: list[Position], now_ms: int) -> list[SyncIssue]:
        issues: list[SyncIssue] = []
        live = [p for p in positions if p.status == "open"]
        if not live:
            return issues
        try:
            totals = self._balance().get("total") or {}
        except ccxt.BaseError as exc:
            return [SyncIssue(live[0], "error", f"balance unavailable: {exc}")]
        for pos in live:
            sym, base = pos.symbol, self._base(pos.symbol)
            held = float(totals.get(base) or 0.0)
            if pos.sl_order_id and not pos.closing_reason:
                try:
                    o = self._fetch(pos.sl_order_id, sym, stop=True)
                except ccxt.BaseError as exc:
                    issues.append(SyncIssue(pos, "error", f"could not check the exchange stop: {exc}"))
                    continue
                filled, status = float(o.get("filled") or 0.0), o.get("status")
                if filled > 0 and status in FINAL:
                    left = pos.open_amount - filled
                    if held > left + max(0.02 * pos.open_amount, 1e-12):  # order says sold, coins say not
                        issues.append(SyncIssue(pos, "error", f"stop {pos.sl_order_id} reports {filled:g} sold "
                                                               f"but the account still holds {held:g} {base}"))
                        continue
                    self._apply(pos, o, "stop")
                    pos.sl_order_id = pos.sl_client_id = None
                    if self._is_dust(sym, pos.open_amount, pos.initial_stop):
                        finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, "stop_loss", now_ms)
                    else:
                        pos.closing_reason = "stop_loss"  # sell what the stop left
                    continue
                if status in FINAL:  # cancelled/expired/rejected without filling
                    pos.sl_order_id = pos.sl_client_id = None
                    try:
                        self.protect(pos, now_ms)
                    except ProtectionError as exc:
                        issues.append(SyncIssue(pos, "unprotected", f"exchange stop was {status} and could not be replaced: {exc}"))
                    continue
                held += filled  # a stop-limit partly filled and still working is ours
            if held < pos.open_amount * 0.98:
                issues.append(SyncIssue(pos, "mismatch", f"account holds {held:g} {base} but the bot expects {pos.open_amount:g}"))
        return issues

    def cancel_orphans(self, symbols: set[str], known_ids: set[str]) -> list[str]:
        """Cancel open orders the bot created (client id prefix) that no position owns."""
        cancelled = []
        for sym in sorted(symbols):
            try:
                orders = with_retries(self.ex.fetch_open_orders, sym, None, None, dict(self.venue.stop_fetch))
            except ccxt.BaseError as exc:
                log.warning("orphan check for %s failed: %s", sym, exc)
                continue
            for o in orders:
                cid = str(o.get("clientOrderId") or "")
                if cid.startswith(BOT_PREFIX) and str(o.get("id")) not in known_ids:
                    self._cancel(str(o["id"]), sym, stop=True)
                    cancelled.append(f"{sym} order {o['id']} ({cid})")
        return cancelled
