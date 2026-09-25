"""Live spot trading through ccxt.

Every step re-reads the exchange instead of assuming what happened:

  entry    The bot persists a *pending* position, then this broker sends a market
           buy tagged with a client order id. A network error leaves the outcome
           unknown, so the balance is re-read: coins that arrived are adopted.
           Unfilled remainders are cancelled; the position size is the final
           filled amount net of any fee taken in the coin itself.
  protect  A stop-loss order is placed on the exchange, fetched back and checked:
           it must be open, carry our trigger price and not have executed. If it
           can't be verified the position is reported unprotected (the bot then
           sells it, unless configured otherwise).
  exit     Cancel the exchange stop, fetch its final state, then sell only what
           is still held - a stop that fills during cancellation can never cause
           a second sale. Partial fills are recorded and the remainder retried.
  sync     Each poll: record a stop that filled, re-place one that vanished, and
           flag positions whose coins are no longer in the account.

The breakeven stop is enforced by the bot; the exchange stop stays at the initial
level as the safety net, so there is no cancel-then-replace window.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import ccxt

from ..data.exchange import ExchangeClient, with_retries
from ..models import Position
from .base import Broker, ExecutionError, NotFilled, ProtectionError, SyncIssue, finalize_close

log = logging.getLogger(__name__)

FINAL = ("closed", "canceled", "expired", "rejected")


@dataclass(frozen=True)
class Venue:
    """Extra ccxt parameters needed to address orders on one exchange."""

    order_fetch: dict = field(default_factory=dict)
    stop_create: dict = field(default_factory=dict)
    stop_fetch: dict = field(default_factory=dict)
    stop_cancel: dict = field(default_factory=dict)


# Routing checked against ccxt's adapters for orders created with `stopLossPrice`.
# Other exchanges are refused for live trading until their stop lifecycle is verified.
VENUES: dict[str, Venue] = {
    # STOP_LOSS / STOP_LOSS_LIMIT are ordinary spot orders
    "binance": Venue(),
    # stop-loss / stop-loss-limit are ordinary orders
    "kraken": Venue(),
    # stop-losses are "algo" orders: fetching and cancelling them needs trigger=True
    "okx": Venue(stop_fetch={"trigger": True}, stop_cancel={"trigger": True}),
    # spot stop-losses live under orderFilter=tpslOrder; unified accounts need
    # `acknowledged` to fetch any order
    "bybit": Venue(
        order_fetch={"acknowledged": True},
        stop_fetch={"acknowledged": True, "orderFilter": "tpslOrder"},
        stop_cancel={"orderFilter": "tpslOrder"},
    ),
}


def _client_id(kind: str, pos: Position) -> str:
    # short and alphanumeric: valid on every supported exchange (OKX allows no dashes,
    # Kraken free-text ids are limited to 18 characters)
    return f"tb{kind}{pos.id or 0}t{int(time.time() * 1000) % 10**9}"


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, client: ExchangeClient, quote: str, native_stop_loss: bool = True,
                 fill_timeout_s: float = 30.0, sleep=time.sleep):
        venue = VENUES.get(client.id)
        if venue is None:
            raise ValueError(
                f"Live trading is not supported on {client.id!r} yet - its stop-loss order handling "
                f"has not been verified. Supported: {', '.join(sorted(VENUES))}."
            )
        self.client = client
        self.ex = client.ex
        self.venue = venue
        self.quote = quote
        self.native_stop_loss = native_stop_loss
        self.fill_timeout_s = fill_timeout_s
        self.sleep = sleep

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

    def _cancel(self, order_id: str, symbol: str, stop: bool = False) -> None:
        params = dict(self.venue.stop_cancel) if stop else {}
        try:
            self.ex.cancel_order(order_id, symbol, params)
        except ccxt.OrderNotFound:
            pass  # already filled or gone - the fetch that follows tells which
        except ccxt.NetworkError as exc:
            log.warning("cancel %s on %s failed (%s); state is re-read next", order_id, symbol, exc)

    def _settle(self, order: dict, symbol: str) -> dict:
        """Wait for an order to finish; cancel any unfilled remainder; return final state."""
        for _ in range(int(self.fill_timeout_s)):
            if order.get("status") in FINAL:
                return order
            self.sleep(1.0)
            order = self._fetch(order["id"], symbol)
        if order.get("status") not in FINAL:
            self._cancel(order["id"], symbol)
            order = self._fetch(order["id"], symbol)
            if order.get("status") not in FINAL:
                raise ExecutionError(f"order {order['id']} on {symbol} still {order.get('status')} after cancelling")
        return order

    def _avg_price(self, order: dict, fallback: float) -> float:
        filled = float(order.get("filled") or 0.0)
        if order.get("average"):
            return float(order["average"])
        if order.get("cost") and filled:
            return float(order["cost"]) / filled
        return float(order.get("price") or fallback)

    def _fees(self, order: dict, base: str, avg: float) -> tuple[float, float]:
        """-> (fee taken in the base coin, all fees converted to quote)."""
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
            else:  # e.g. BNB fee discount
                try:
                    quote_fee += cost * self.client.fetch_last_price(f"{cur}/{self.quote}")
                except Exception:
                    log.warning("could not value a %s %s fee in %s", cost, cur, self.quote)
        return base_fee, quote_fee

    def _filled(self, order: dict) -> float:
        filled = order.get("filled")
        if filled is None and order.get("status") == "closed":
            filled = order.get("amount")
        return float(filled or 0.0)

    def _record_exit(self, pos: Position, order: dict) -> None:
        filled = self._filled(order)
        if filled <= 0:
            return
        avg = self._avg_price(order, pos.stop_loss)
        _, quote_fee = self._fees(order, self._base(pos.symbol), avg)
        pos.exit_filled += filled
        pos.exit_value += filled * avg
        pos.fees += quote_fee

    def _is_dust(self, symbol: str, qty: float, price: float) -> bool:
        if qty <= 0 or self.client.amount_to_precision(symbol, qty) <= 0:
            return True
        lim = self.client.limits(symbol)
        return qty < (lim.get("min_amount") or 0.0) or qty * price < (lim.get("min_cost") or 0.0)

    def _arrived(self, base: str, held_before: float) -> float:
        """After an ambiguous buy, how many coins actually arrived?"""
        for _ in range(3):
            self.sleep(2.0)
            try:
                held = self._held(base)
            except ccxt.BaseError:
                continue
            if held > held_before:
                return held - held_before
        return 0.0

    # ------------------------------------------------------------ balances
    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        totals = self._balance().get("total") or {}
        value = float(totals.get(self.quote) or 0.0)
        for p in positions:
            held = float(totals.get(self._base(p.symbol)) or 0.0)
            # never count coins the account doesn't actually hold
            value += min(p.open_amount, held) * prices.get(p.symbol, p.entry_price)
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
        sym, base = pos.symbol, self._base(pos.symbol)
        amount = self.client.amount_to_precision(sym, pos.amount)
        try:
            held_before = self._held(base)
        except ccxt.BaseError as exc:
            raise NotFilled(f"could not read the balance before buying: {exc}") from exc
        pos.client_order_id = _client_id("e", pos)
        order = None
        try:
            order = self.ex.create_order(sym, "market", "buy", amount, None, {"clientOrderId": pos.client_order_id})
        except ccxt.NetworkError as exc:  # the order may or may not exist
            log.error("entry for %s: %s - checking the balance", sym, exc)
        except ccxt.BaseError as exc:  # rejected: nothing was bought
            raise NotFilled(f"entry rejected by {self.client.id}: {exc}") from exc
        if order is not None:
            try:
                order = self._settle(order, sym)
            except (ccxt.BaseError, ExecutionError) as exc:
                log.error("entry %s for %s could not be confirmed (%s) - checking the balance",
                          order.get("id"), sym, exc)
                pos.entry_order_id = str(order.get("id") or "")
                order = None

        if order is None:
            arrived = self._arrived(base, held_before)
            if arrived <= 0:
                raise ExecutionError(
                    f"entry for {sym} has an unknown outcome and no {base} has arrived yet "
                    f"(client order id {pos.client_order_id})"
                )
            pos.amount, pos.entry_price, pos.fees = arrived, price, 0.0  # price is an estimate
        else:
            filled = self._filled(order)
            if filled <= 0:
                raise NotFilled(f"entry order {order.get('id')} filled nothing ({order.get('status')})")
            avg = self._avg_price(order, price)
            base_fee, quote_fee = self._fees(order, base, avg)
            pos.amount = filled - base_fee  # coins actually received
            pos.entry_price = avg
            pos.fees = quote_fee  # includes the coin fee valued in quote
            pos.entry_order_id = str(order.get("id") or "")
        pos.opened_at = now_ms
        pos.status = "open"
        return pos

    # ---------------------------------------------------------- protection
    def protect(self, pos: Position, now_ms: int) -> None:
        if not self.native_stop_loss:
            return
        sym = pos.symbol
        qty = self.client.amount_to_precision(sym, pos.open_amount)
        stop = self.client.price_to_precision(sym, pos.initial_stop)
        problems = []
        for otype, limit in (("market", None), ("limit", self.client.price_to_precision(sym, stop * 0.995))):
            try:
                order = self.ex.create_order(sym, otype, "sell", qty, limit,
                                             {"stopLossPrice": stop, **self.venue.stop_create})
            except ccxt.NetworkError as exc:
                raise ProtectionError(f"stop order outcome unknown: {exc}") from exc
            except ccxt.BaseError as exc:
                problems.append(f"{otype} stop rejected: {exc}")
                continue
            state, detail = self._verify_stop(order, sym, stop)
            if state == "ok":
                pos.sl_order_id = str(order["id"])
                log.info("verified %s stop for %s %s @ %s (order %s)", otype, qty, sym, stop, order["id"])
                return
            if state == "executed":  # the exchange treated it as a plain sell
                self._record_exit(pos, detail)
                if self._is_dust(sym, pos.open_amount, pos.initial_stop):
                    finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, "stop_order_executed", now_ms)
                    return
                raise ProtectionError("the stop order executed immediately for part of the position")
            self._cancel(order["id"], sym, stop=True)
            self._cancel(order["id"], sym, stop=False)
            problems.append(f"{otype} stop not protective ({detail})")
        raise ProtectionError("; ".join(problems))

    def _verify_stop(self, order: dict, symbol: str, stop: float) -> tuple[str, object]:
        oid = order.get("id")
        if not oid:
            return "bad", "no order id returned"
        fetched = None
        for as_stop in (True, False):
            try:
                fetched = self._fetch(oid, symbol, stop=as_stop)
                break
            except ccxt.OrderNotFound:
                continue
            except ccxt.BaseError as exc:
                raise ProtectionError(f"could not verify stop order {oid}: {exc}") from exc
        if fetched is None:
            return "bad", "order not found"
        if self._filled(fetched) > 0 or fetched.get("status") == "closed":
            return "executed", fetched
        if fetched.get("status") != "open":
            return "bad", f"status {fetched.get('status')!r}"
        trig = next((fetched.get(k) for k in ("triggerPrice", "stopLossPrice", "stopPrice") if fetched.get(k)), None)
        if trig is None:
            return "bad", "no trigger price on the order"
        if abs(float(trig) - stop) > 0.002 * stop:
            return "bad", f"trigger {trig} differs from {stop}"
        if (fetched.get("side") or "sell") != "sell":
            return "bad", f"side {fetched.get('side')}"
        return "ok", fetched

    # ---------------------------------------------------------------- exit
    def _release_stop(self, pos: Position) -> bool:
        """Cancel the exchange stop and record whatever it already sold."""
        oid, sym = pos.sl_order_id, pos.symbol
        self._cancel(oid, sym, stop=True)
        try:
            final = self._fetch(oid, sym, stop=True)
        except ccxt.BaseError as exc:
            raise ExecutionError(
                f"could not confirm the exchange stop {oid} for {sym} is cancelled ({exc}); "
                f"not selling to avoid a double sale"
            ) from exc
        if final.get("status") not in FINAL:
            raise ExecutionError(f"exchange stop {oid} for {sym} is still {final.get('status')}; not selling")
        pos.sl_order_id = None
        before = pos.exit_filled
        self._record_exit(pos, final)
        return pos.exit_filled > before

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        sym, base = pos.symbol, self._base(pos.symbol)
        pos.closing_reason = reason
        if pos.sl_order_id and self._release_stop(pos) and self._is_dust(sym, pos.open_amount, price):
            reason = "stop_loss"  # the exchange stop got there first and sold everything
        if not self._is_dust(sym, pos.open_amount, price):
            self._sell_remaining(pos, price)
        if not self._is_dust(sym, pos.open_amount, price):
            raise ExecutionError(f"{pos.open_amount:g} {base} still held after selling; will retry")
        exit_price = pos.exit_value / pos.exit_filled if pos.exit_filled else price
        return finalize_close(pos, exit_price, 0.0, reason, now_ms)

    def _sell_remaining(self, pos: Position, price: float) -> None:
        sym, base = pos.symbol, self._base(pos.symbol)
        held = self._held(base, "free")
        qty = self.client.amount_to_precision(sym, min(pos.open_amount, held))
        if self._is_dust(sym, qty, price):
            raise ExecutionError(
                f"the bot expects {pos.open_amount:g} {base} but only {held:g} is free - "
                f"sold outside the bot? Not selling. Check the account, then /forget {pos.id}"
            )
        held_total = self._held(base)
        order = None
        try:
            order = self.ex.create_order(sym, "market", "sell", qty, None, {"clientOrderId": _client_id("x", pos)})
        except ccxt.NetworkError as exc:  # the sell may or may not have happened
            log.error("exit for %s: %s - checking the balance", sym, exc)
        except ccxt.BaseError as exc:
            raise ExecutionError(f"sell of {qty:g} {base} rejected: {exc}") from exc
        if order is not None:
            try:
                order = self._settle(order, sym)
            except (ccxt.BaseError, ExecutionError) as exc:
                log.error("exit %s for %s unconfirmed (%s) - checking the balance", order.get("id"), sym, exc)
                order = None
        if order is None:
            self.sleep(2.0)
            order = {"filled": max(held_total - self._held(base), 0.0), "average": price}
        self._record_exit(pos, order)

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
            sym = pos.symbol
            sold_by_stop = 0.0  # a stop-limit can be partly filled and still open
            if pos.sl_order_id:
                try:
                    o = self._fetch(pos.sl_order_id, sym, stop=True)
                except ccxt.BaseError as exc:
                    issues.append(SyncIssue(pos, "error", f"could not check the exchange stop: {exc}"))
                    continue
                status = o.get("status")
                if status == "closed" or (status in FINAL and self._filled(o) > 0):
                    pos.sl_order_id = None
                    self._record_exit(pos, o)
                    if self._is_dust(sym, pos.open_amount, pos.initial_stop):
                        finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, "stop_loss", now_ms)
                    else:
                        pos.closing_reason = "stop_loss"  # sell what the stop left over
                    continue
                if status in FINAL:  # cancelled/expired/rejected without filling
                    pos.sl_order_id = None
                    try:
                        self.protect(pos, now_ms)
                    except ProtectionError as exc:
                        issues.append(SyncIssue(pos, "unprotected", f"exchange stop was {status} and could not be replaced: {exc}"))
                    continue
                sold_by_stop = self._filled(o)
            held = float(totals.get(self._base(sym)) or 0.0)
            if held < (pos.open_amount - sold_by_stop) * 0.98:
                issues.append(SyncIssue(
                    pos, "mismatch",
                    f"account holds {held:g} {self._base(sym)} but the bot expects {pos.open_amount:g}",
                ))
        return issues
