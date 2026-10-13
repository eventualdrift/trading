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
  * from entry until the stop first moves, the stop and the take-profit rest on the
    exchange together as an OCO pair (one quantity: whichever triggers expires the other);
  * the stop is moved (breakeven, then trailing) in ONE cancel-replace request: the
    exchange cancels the protection and places the new stop straight after, so no moment
    passes between two requests with no stop. Spot coins can carry one sell order and an
    OCO can't be replaced by an OCO in one request, so the take-profit leaves the exchange
    at the first move and the bot takes it from then on; keeping it would mean a gap of a
    round trip (about 0.4 s from the box) with no stop on every move;
  * a move is saved before it is sent; if its answer is lost, the new stop is looked up by
    its client id and the old order read back, and a position the move left without a
    stop gets one at once or is sold.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

import ccxt

from ..data.exchange import ExchangeClient, with_retries
from ..models import Position, new_client_id
from .base import Broker, ExecutionError, NotFilled, ProtectionError, SyncIssue, finalize_close

log = logging.getLogger(__name__)

FINAL = ("closed", "canceled", "expired", "rejected")
# exits that rest on the exchange as orders, so they still fire while the bot is down. The
# go-live checklist (report.readiness) requires every protective exit to be here.
EXCHANGE_SIDE_EXITS = frozenset({"stop_loss", "breakeven_stop", "trailing_stop"})
# exits on the exchange only for part of a trade, and why
PARTLY_EXCHANGE_SIDE = {
    "take_profit": "on the exchange with the stop (an OCO pair) until the stop first moves; after that the bot "
                   "takes it (Binance spot can't keep it across a stop move without a moment with no stop)",
}
MIN_MOVE_R = 0.1  # the exchange stop follows the bot's trailing level in steps of at least this much risk
BOT_PREFIX = "tb"


class FeesUnknown(ExecutionError):
    """An order filled but its fees can't be read yet (its trades are unavailable): retried."""


def _error_body(exc: Exception) -> dict:
    """The JSON body of a Binance error ccxt raised ('binance {...}'); {} when the message has none.
    Never the client's last response: after a timeout that is an earlier request's."""
    text = str(exc)
    start = text.find("{")
    if start >= 0:
        try:
            body = json.loads(text[start:])
            return body if isinstance(body, dict) else {}
        except ValueError:
            pass
    return {}


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
                 fill_timeout_s: float = 30.0, sleep=time.sleep, clock=None, persist=None,
                 testnet: bool = False, take_profit_orders: bool = True, id_tag: str = ""):
        """``testnet``: the exchange's testnet only - refused unless the client points there
        (mode: testnet). Live trading itself is refused by the config (config.LIVE_TRADING)."""
        venue = VENUES.get(client.id)
        if venue is None:
            raise ValueError(
                f"Live trading is not supported on {client.id!r}: its order handling has not been verified. "
                f"Supported: {', '.join(sorted(VENUES))}. Signals and paper trading work on any exchange."
            )
        self.testnet = testnet
        if testnet:
            client.assert_testnet()
            self.mode = "testnet"
        self.client = client
        self.ex = client.ex
        self.venue = venue
        # the core sleeve's (cash, coins by base currency) in the same account: not the satellite's
        self.reserved = lambda: (0.0, {})
        self.quote = quote
        self.native_stop_loss = native_stop_loss
        self.take_profit_orders = take_profit_orders  # the take-profit on the exchange with the stop (OCO)
        self.id_tag = id_tag
        self.fill_timeout_s = fill_timeout_s
        self.sleep = sleep
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.persist = persist or (lambda pos: None)
        self.recv_window_ms = int((getattr(self.ex, "options", None) or {}).get("recvWindow", 10_000))

    # ------------------------------------------------------------- helpers
    def _amount(self, symbol: str, amount: float) -> float:
        """The amount rounded down to the exchange's lot step; 0 when under one step (ccxt raises)."""
        if amount <= 0:
            return 0.0
        try:
            return self.client.amount_to_precision(symbol, amount)
        except ccxt.InvalidOrder:
            return 0.0

    def client_id(self, kind: str) -> str:
        """A new client order id; ``id_tag`` marks a run's own orders (testnet verify: 'v')."""
        return new_client_id(self.id_tag + kind)

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
        except ccxt.OperationFailed as exc:
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

    def _order_fees(self, order: dict, symbol: str) -> list[dict]:
        """The fees an order paid. A create answer carries them; an order read back (GET order: a
        lookup after a lost answer, an exchange stop that fired) carries none, so they come from
        its trades - otherwise a buy would be booked with the coins the fee took."""
        fees = list(order.get("fees") or []) or ([order["fee"]] if order.get("fee") else [])
        if fees or not float(order.get("filled") or 0.0) or not order.get("id"):
            return fees
        try:
            trades = with_retries(self.ex.fetch_my_trades, symbol, None, None, {"orderId": str(order["id"])})
        except Exception as exc:  # noqa: BLE001 - the caller decides whether it can book without them
            raise FeesUnknown(f"fees of order {order.get('id')} on {symbol} unknown ({exc})") from exc
        return [t["fee"] for t in trades or [] if t.get("fee")]

    def _fees(self, order: dict, base: str, avg: float, symbol: str | None = None) -> tuple[float, float]:
        """-> (fee taken in the base coin, all fees valued in quote)."""
        return self._fee_parts(order, base, avg, symbol)[:2]

    def _fee_parts(self, order: dict, base: str, avg: float, symbol: str | None = None) -> tuple[float, float, float]:
        """-> (fee taken in the base coin, all fees valued in quote, the part paid in quote itself)."""
        fees = (self._order_fees(order, symbol) if symbol else
                list(order.get("fees") or []) or ([order["fee"]] if order.get("fee") else []))
        base_fee = quote_fee = paid_in_quote = 0.0
        for f in fees:
            if not f or not f.get("cost"):
                continue
            cost, cur = float(f["cost"]), f.get("currency")
            if cur == base:
                base_fee += cost
                quote_fee += cost * avg
            elif cur in (self.quote, None):
                quote_fee += cost
                paid_in_quote += cost
            else:  # e.g. a BNB fee discount
                try:
                    quote_fee += cost * self.client.fetch_last_price(f"{cur}/{self.quote}")
                except Exception:
                    log.warning("could not value a %s %s fee in %s", cost, cur, self.quote)
        return base_fee, quote_fee, paid_in_quote

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
        prev = pos.fills.get(str(order["id"]))
        if prev and prev.get("filled") == filled and not (order.get("fees") or order.get("fee")):
            base_fee, quote_fee = prev["base_fee"], prev["fee"]  # read before: its fees are known
        else:
            try:
                base_fee, quote_fee = self._fees(order, self._base(pos.symbol), avg, pos.symbol)
            except FeesUnknown as exc:
                if kind == "entry":  # a buy's fee is taken in coins: the amount held depends on it
                    raise
                log.warning("%s: booked without its fee", exc)  # an exit's fee is quote: only P&L is off
                base_fee = quote_fee = 0.0
        fills = dict(pos.fills)
        fills[str(order["id"])] = {"kind": kind, "filled": filled, "avg": avg, "base_fee": base_fee, "fee": quote_fee}
        pos.fills = fills
        entry = [f for f in fills.values() if f["kind"] == "entry"]
        exits = [f for f in fills.values() if f["kind"] in ("exit", "stop", "tp")]
        if entry:
            gross = sum(f["filled"] for f in entry)
            if gross > 0:
                pos.entry_price = sum(f["filled"] * f["avg"] for f in entry) / gross
            pos.amount = gross - sum(f["base_fee"] for f in entry)  # coins actually received
        pos.exit_filled = sum(f["filled"] for f in exits)
        pos.exit_value = sum(f["filled"] * f["avg"] for f in exits)
        pos.fees = sum(f["fee"] for f in fills.values())

    def _is_dust(self, symbol: str, qty: float, price: float) -> bool:
        if qty <= 0 or self._amount(symbol, qty) <= 0:
            return True
        lim = self.client.limits(symbol)
        return qty < (lim.get("min_amount") or 0.0) or qty * price < (lim.get("min_cost") or 0.0)

    # ------------------------------------------------------------ balances
    def _satellite_coins(self, base: str, balances: dict) -> float:
        """Coins of ``base`` in ``balances`` that are not the core sleeve's."""
        return max(float(balances.get(base) or 0.0) - self.reserved()[1].get(base, 0.0), 0.0)

    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        """The satellite's equity: the quote balance less the core's cash, plus the coins of its
        positions really held (the core's coins of the same currency left out)."""
        totals = self._balance().get("total") or {}
        value = float(totals.get(self.quote) or 0.0) - self.reserved()[0]
        left: dict[str, float] = {}
        for p in positions:
            base = self._base(p.symbol)
            held = left.get(base, self._satellite_coins(base, totals))
            mine = min(p.open_amount, held)  # only coins really held
            left[base] = held - mine
            value += mine * prices.get(p.symbol, p.entry_price)
        return value

    def available_cash(self) -> float | None:
        return max(self._held(self.quote, "free") - self.reserved()[0], 0.0)

    def free_coins(self, symbol: str) -> float:
        """Coins of the symbol's base currency free on the account (not locked by an order)."""
        return self._held(self._base(symbol), "free")

    def transfer(self, delta: float) -> None:
        """Core/satellite moves are bookkeeping in one account: the core's cash ledger is what is
        reserved (``reserved``), so the satellite's equity changes with it."""

    def limits(self, symbol: str) -> dict:
        return self.client.limits(symbol)

    def to_precision(self, symbol: str):
        return lambda a: self._amount(symbol, a)

    # --------------------------------------------------------------- entry
    def open_position(self, pos: Position, price: float, now_ms: int) -> Position:
        if pos.side != "long":
            raise NotFilled("live spot trading is long-only")
        if not pos.client_order_id:
            raise ValueError("position needs a client_order_id persisted before the order is sent")
        sym = pos.symbol
        amount = self._amount(sym, pos.amount)
        sent = self.clock()
        try:
            order = self.ex.create_order(sym, "market", "buy", amount, None, {"clientOrderId": pos.client_order_id})
        except ccxt.OperationFailed as exc:  # the order may or may not exist (network, -1006 status unknown)
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
    def _protection(self, pos: Position) -> dict:
        return dict(pos.protection or {})

    def _set_protection(self, pos: Position, **values) -> None:
        prot = self._protection(pos)
        prot.update(values)
        pos.protection = {k: v for k, v in prot.items() if v is not None}

    def _clear_protection(self, pos: Position) -> None:
        """No order protects the position any more (it executed, was cancelled or vanished). Whether
        its stop had moved is kept: a moved stop is never paired with a take-profit again."""
        pos.sl_order_id = pos.sl_client_id = None
        prot = self._protection(pos)
        pos.protection = {k: v for k, v in prot.items() if k in ("moved", "stop")}  # the baseline goes with it

    def _stop_exit_reason(self, pos: Position) -> str:
        """Why a position whose exchange stop executed was closed: the stop's level decides."""
        prot = self._protection(pos)
        if not prot.get("moved"):
            return "stop_loss"
        level = float(prot.get("stop", pos.stop_loss))
        beyond = level > pos.entry_price * (1 + 1e-9) if pos.side == "long" else level < pos.entry_price * (1 - 1e-9)
        return "trailing_stop" if beyond else "breakeven_stop"

    def _baseline(self, pos: Position) -> float | None:
        """The satellite's coins of this currency on the account once the protection stands (other
        coins of it - a testnet's starting balance, dust - included): what an exit fill is checked
        against. None if the balance can't be read."""
        try:
            return self._satellite_coins(self._base(pos.symbol), self._balance().get("total") or {})
        except Exception:  # noqa: BLE001 - only the sync check is weaker without it
            return None

    def _wants_take_profit(self, pos: Position) -> bool:
        return (self.take_profit_orders and callable(getattr(self.ex, "privatePostOrderListOco", None))
                and not pos.breakeven_moved and not self._protection(pos).get("moved")
                and bool(pos.take_profit) and pos.take_profit > pos.stop_loss)

    def protect(self, pos: Position, now_ms: int) -> None:
        """Put the position's exits on the exchange and check them there. Until the stop first
        moves: an OCO pair - a take-profit trigger at the target and a stop-loss at the stop - so
        both fire while the bot is down. Once it has moved: a single stop-loss at the bot's level
        (an OCO can't be moved without a moment with no stop: see ``sync_stop``). A refused pair
        falls back to a single stop; ProtectionError when no stop could be placed."""
        if not self.native_stop_loss:
            return
        sym = pos.symbol
        qty = self._amount(sym, pos.open_amount)
        stop = self.client.price_to_precision(sym, pos.stop_loss)
        held = self._baseline(pos)  # before anything is placed: an exit that fires later is measured from it
        problems = []
        if self._wants_take_profit(pos):
            problem = self._protect_oco(pos, qty, stop, now_ms, held)
            if problem is None:  # in place and checked, or the position closed
                return
            problems.append(problem)
        for otype, limit in (("market", None), ("limit", self.client.price_to_precision(sym, stop * 0.995))):
            pos.sl_client_id, pos.sl_sent_ms = self.client_id("s"), self.clock()
            self.persist(pos)
            params = {"stopLossPrice": stop, "clientOrderId": pos.sl_client_id, **self.venue.stop_create}
            try:
                order = self.ex.create_order(sym, otype, "sell", qty, limit, params)
            except ccxt.OperationFailed as exc:  # outcome unknown
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
                self._set_protection(pos, stop=stop, moved=bool(self._protection(pos).get("moved") or pos.breakeven_moved)
                                     or None, tp_order_id=None, tp_client_id=None, list_id=None, list_client_id=None,
                                     held=held)
                self.persist(pos)
                log.info("verified %s stop for %s %s @ %s (order %s)", otype, qty, sym, stop, order["id"])
                return
            if state == "executed":  # the exchange sold instead of waiting for the trigger
                self._apply(pos, detail, "stop")
                pos.sl_client_id = None
                if self._is_dust(sym, pos.open_amount, stop):
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

    def _protect_oco(self, pos: Position, qty: float, stop: float, now_ms: int, held: float | None = None) -> str | None:
        """The OCO pair: a TAKE_PROFIT above the market and a STOP_LOSS below it, one quantity.
        None: in place and checked (or the position closed); else why it isn't (the caller falls
        back to a single stop). Raises ProtectionError if a leg executed for part of the position."""
        sym = pos.symbol
        tp = self.client.price_to_precision(sym, pos.take_profit)
        lcid, tcid, scid = self.client_id("l"), self.client_id("t"), self.client_id("s")
        pos.sl_client_id, pos.sl_sent_ms = scid, self.clock()
        self._set_protection(pos, list_client_id=lcid, tp_client_id=tcid)
        self.persist(pos)  # the ids are saved BEFORE the pair is sent
        params = {"symbol": self.client.market(sym)["id"], "side": "SELL",
                  "quantity": self.ex.amount_to_precision(sym, qty),
                  "aboveType": "TAKE_PROFIT", "aboveStopPrice": self.ex.price_to_precision(sym, tp),
                  "belowType": "STOP_LOSS", "belowStopPrice": self.ex.price_to_precision(sym, stop),
                  "listClientOrderId": lcid, "aboveClientOrderId": tcid, "belowClientOrderId": scid}
        list_id = None
        try:
            resp = self.ex.privatePostOrderListOco(params)
            ids = {o.get("clientOrderId"): str(o.get("orderId")) for o in (resp or {}).get("orders") or []}
            list_id = (resp or {}).get("orderListId")
            stop_leg = self._fetch(ids[scid], sym, stop=True)
            tp_leg = self._fetch(ids[tcid], sym, stop=True)
        except ccxt.OperationFailed as exc:  # answer lost or status unknown: found by the legs' client ids, or never existed
            try:
                stop_leg = self._lookup(sym, scid, pos.sl_sent_ms, stop=True)
                tp_leg = self._lookup(sym, tcid, pos.sl_sent_ms, stop=True) if stop_leg is not None else None
            except ccxt.BaseError as exc2:
                raise ProtectionError(f"OCO outcome unknown ({exc}; lookup: {exc2})") from exc2
            if stop_leg is None:
                self._clear_protection(pos)
                return f"OCO not placed ({exc})"
            if tp_leg is None:  # can't happen for a pair; treat the stop leg as a lone order to replace
                tp_leg = {"id": None, "status": "canceled", "filled": 0.0}
        except (ccxt.BaseError, KeyError) as exc:
            self._clear_protection(pos)
            return f"OCO refused: {exc}"
        s_state, s_detail = self._verify_stop(stop_leg, sym, stop, qty)
        t_state, t_detail = (self._verify_stop(tp_leg, sym, tp, qty) if tp_leg.get("id")
                             else ("bad", "take-profit leg missing"))
        if "executed" in (s_state, t_state):  # the market moved through a level as the pair was placed
            for leg, kind in ((stop_leg, "stop"), (tp_leg, "tp")):
                fresh = self._fetch(str(leg["id"]), sym, stop=True) if leg.get("id") else {}
                if float(fresh.get("filled") or 0.0) > 0:
                    self._apply(pos, fresh, kind)
            self._clear_protection(pos)
            if self._is_dust(sym, pos.open_amount, stop):
                reason = "take_profit" if t_state == "executed" else "stop_order_executed"
                finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, reason, now_ms)
                return None
            raise ProtectionError("an exit order executed immediately for part of the position")
        if s_state == "ok" and t_state == "ok":
            pos.sl_order_id = str(stop_leg["id"])
            self._set_protection(pos, list_id=list_id, tp_order_id=str(tp_leg["id"]), stop=stop, tp=tp, moved=None,
                                 held=held)
            self.persist(pos)
            log.info("verified OCO for %s %s: stop %s, take-profit %s (orders %s, %s)", qty, sym, stop, tp,
                     stop_leg["id"], tp_leg["id"])
            return None
        self._cancel(str(stop_leg["id"]), sym, stop=True)  # one leg cancels the pair
        for leg, kind in ((stop_leg, "stop"), (tp_leg, "tp")):
            if leg.get("id"):
                final = self._fetch(str(leg["id"]), sym, stop=True)
                if final.get("status") not in FINAL:
                    raise ProtectionError(f"a non-protective order {leg['id']} could not be cancelled")
                self._apply(pos, final, kind)
        self._clear_protection(pos)
        return f"OCO not protective (stop: {s_detail if s_state != 'ok' else 'ok'}; take-profit: " \
               f"{t_detail if t_state != 'ok' else 'ok'})"

    def _verify_stop(self, order: dict, symbol: str, stop: float, qty: float) -> tuple[str, object]:
        try:
            o = self._fetch(order["id"], symbol, stop=True)
        except ccxt.BaseError as exc:
            raise ProtectionError(f"could not read back order {order.get('id')}: {exc}") from exc
        if float(o.get("filled") or 0.0) > 0:
            return "executed", o
        if o.get("status") != "open":
            return "bad", f"status {o.get('status')!r}"
        if o.get("symbol") not in (None, symbol):
            return "bad", f"symbol {o.get('symbol')}"
        if (o.get("side") or "sell") != "sell":
            return "bad", f"side {o.get('side')}"
        trig = next((o.get(k) for k in ("triggerPrice", "stopLossPrice", "stopPrice", "takeProfitPrice") if o.get(k)), None)
        if trig is None or abs(float(trig) - stop) > 0.002 * stop:
            return "bad", f"trigger {trig} instead of {stop}"
        remaining = float(o.get("remaining") if o.get("remaining") is not None else (o.get("amount") or 0.0))
        if remaining < qty * 0.999:
            return "bad", f"covers {remaining:g} of {qty:g}"
        return "ok", o

    # ---------------------------------------------------------- moving the stop
    def sync_stop(self, pos: Position, now_ms: int) -> None:
        """Bring the exchange stop up to the bot's level (breakeven, then trailing) in ONE request:
        Binance's cancel-replace cancels the order that protects the position - a single stop, or
        the OCO pair through its stop leg - and places the new single stop straight after, in the
        same request. No moment passes between two requests with nothing protecting the coins.
        The take-profit leaves the exchange at the first move (an OCO can't be replaced by an OCO in
        one request, and spot coins can't carry two sell orders): the bot takes it from then on.
        Trailing steps smaller than MIN_MOVE_R of the initial risk wait for a bigger one; while the
        bot runs it enforces its own level, so the exchange stop only has to be close."""
        if not self.native_stop_loss or pos.status != "open" or pos.closing_reason:
            return
        if self._protection(pos).get("move"):  # a move whose answer was lost: settle it first
            self._resolve_move(pos, now_ms)
            if pos.status != "open" or pos.closing_reason or self._protection(pos).get("move"):
                return
        if not pos.sl_order_id:
            return  # nothing to move: protect() puts a stop in place (start-up, sync)
        sym = pos.symbol
        prot = self._protection(pos)
        exchange_level = float(prot.get("stop", pos.initial_stop))
        want = self.client.price_to_precision(sym, pos.stop_loss)
        if pos.side != "long" or want <= exchange_level:
            return
        per_unit = abs(pos.entry_price - pos.initial_stop)
        to_breakeven = pos.breakeven_moved and exchange_level < pos.entry_price * (1 - 1e-9)
        if not to_breakeven and want - exchange_level < MIN_MOVE_R * per_unit:
            return
        self._replace_stop(pos, want, now_ms)

    def _replace_stop(self, pos: Position, level: float, now_ms: int) -> None:
        sym = pos.symbol
        qty = self._amount(sym, pos.open_amount)
        prot = self._protection(pos)
        move = {"from": pos.sl_order_id, "from_tp": prot.get("tp_order_id"), "cid": self.client_id("s"),
                "level": level, "sent_ms": self.clock()}
        self._set_protection(pos, move=move)
        self.persist(pos)  # what is about to happen is saved BEFORE the request
        params = {"symbol": self.client.market(sym)["id"], "side": "SELL", "type": "STOP_LOSS",
                  "quantity": self.ex.amount_to_precision(sym, qty), "stopPrice": self.ex.price_to_precision(sym, level),
                  "cancelReplaceMode": "STOP_ON_FAILURE", "cancelOrderId": move["from"], "newClientOrderId": move["cid"]}
        try:
            resp = self.ex.privatePostOrderCancelReplace(params)
        except ccxt.BaseError as exc:
            body = _error_body(exc)
            if "cancelResult" in (body.get("data") or {}):  # -2021 / -2022, however ccxt classed it (a
                self._replace_failed(pos, body, now_ms)  # filter failure in the body comes as InvalidOrder)
                return
            if isinstance(exc, ccxt.OperationFailed):  # the answer was lost or unknown: ask the exchange
                log.error("moving the stop of %s: %s - resolving it by order ids", sym, exc)
                self._resolve_move(pos, now_ms)
                return
            self._move_refused(pos, exc, level, now_ms)
            return
        new_id = str(((resp or {}).get("newOrderResponse") or {}).get("orderId"))
        self._adopt_stop(pos, new_id, now_ms)

    def _move_refused(self, pos: Position, exc: Exception, level: float, now_ms: int) -> None:
        """Refused as a whole (a filter checked before anything happens): the old order should
        stand - read back to be sure."""
        sym = pos.symbol
        move = self._protection(pos)["move"]
        log.warning("moving the stop of %s to %s refused: %s", sym, level, exc)
        old = self._fetch(move["from"], sym, stop=True)
        if old.get("status") == "open":
            self._set_protection(pos, move=None)
            self.persist(pos)
        else:
            self._resolve_move(pos, now_ms)

    def _executed_during_move(self, pos: Position, move: dict, legs: list[dict], now_ms: int) -> bool:
        """The old protection sold while it was being moved (its fills are recorded by now): the
        position is closed, or the rest is sold, under the exit that fired. -> True if so."""
        sold = [(o, kind) for o, kind in zip(legs, ("stop", "tp")) if float(o.get("filled") or 0.0) > 0]
        if not sold:
            return False
        reason = "take_profit" if any(kind == "tp" for _, kind in sold) else self._stop_exit_reason(pos)
        self._clear_protection(pos)
        if self._is_dust(pos.symbol, pos.open_amount, pos.stop_loss):
            finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, reason, now_ms)
        else:
            pos.closing_reason = reason
        self.persist(pos)
        return True

    def _old_legs_final(self, pos: Position, move: dict) -> list[dict]:
        """The orders a move replaced, read back and recorded (any fill they made counts)."""
        out = []
        for oid, kind in ((move.get("from"), "stop"), (move.get("from_tp"), "tp")):
            o = self._fetch(str(oid), pos.symbol, stop=True) if oid else {}
            if o.get("status") in FINAL:
                self._apply(pos, o, kind)
            out.append(o)  # [the stop leg, the take-profit leg] - {} where there is none
        return out

    def _adopt_stop(self, pos: Position, new_id: str, now_ms: int) -> None:
        """The replacing stop exists: check it on the exchange and make it the position's protection."""
        sym = pos.symbol
        move = self._protection(pos)["move"]
        self._old_legs_final(pos, move)
        qty = self._amount(sym, pos.open_amount)
        state, detail = self._verify_stop({"id": new_id}, sym, move["level"], qty)
        if state == "ok":
            pos.sl_order_id, pos.sl_client_id = new_id, move["cid"]
            self._set_protection(pos, stop=move["level"], moved=True, move=None, tp_order_id=None, tp_client_id=None,
                                 list_id=None, list_client_id=None)  # a move changes no coins: the baseline stays
            self.persist(pos)
            log.info("moved the exchange stop of %s to %s (order %s)", sym, move["level"], new_id)
            return
        if state == "executed":  # it triggered at once: the bot sells whatever it left
            self._apply(pos, detail, "stop")
            self._set_protection(pos, stop=move["level"], moved=True)
            reason = self._stop_exit_reason(pos)
            self._clear_protection(pos)
            if self._is_dust(sym, pos.open_amount, move["level"]):
                finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, reason, now_ms)
            else:
                pos.closing_reason = reason
            self.persist(pos)
            return
        self._cancel(new_id, sym, stop=True)
        self._apply(pos, self._fetch(new_id, sym, stop=True), "stop")
        self._unprotected_after_move(pos, move, f"the replacing stop is not protective ({detail})", now_ms)

    def _unprotected_after_move(self, pos: Position, move: dict, why: str, now_ms: int) -> None:
        """The old protection is gone and no new stop stands: put one in place at once, or sell."""
        self._set_protection(pos, stop=move["level"], moved=True)
        self._clear_protection(pos)
        log.error("%s: %s - placing a stop again", pos.symbol, why)
        try:
            self.protect(pos, now_ms)
        except ProtectionError as exc:
            pos.closing_reason = "no_protection"
            log.error("%s: no stop could be placed (%s) - selling", pos.symbol, exc)
        self.persist(pos)

    def _replace_failed(self, pos: Position, body: dict, now_ms: int) -> None:
        data = body.get("data") or {}
        move = self._protection(pos)["move"]
        if data.get("cancelResult") != "SUCCESS":  # the old order was no longer open: it executed or vanished
            self._set_protection(pos, move=None)  # sync reads what happened to it
            self.persist(pos)
            return
        legs = self._old_legs_final(pos, move)  # cancelled: the position has no stop now
        if self._executed_during_move(pos, move, legs, now_ms):
            return
        msg = str(((data.get("newOrderResponse") or {}).get("msg")) or "")
        if "trigger immediately" in msg.lower():  # the market is already through the new level: the stop is hit
            self._set_protection(pos, stop=move["level"], moved=True)
            pos.closing_reason = self._stop_exit_reason(pos)
            self._clear_protection(pos)
            self.persist(pos)
            log.warning("%s: price is through the new stop %s - selling", pos.symbol, move["level"])
            return
        self._unprotected_after_move(pos, move, f"new stop refused after the cancel ({msg})", now_ms)

    def _resolve_move(self, pos: Position, now_ms: int) -> None:
        """A move whose outcome is unknown: the new stop is looked up by its client id (it exists,
        or - once the exchange's recvWindow has passed - it was never placed) and the old order is
        read back. Raises while the exchange can't be asked; retried on the next check."""
        sym = pos.symbol
        move = self._protection(pos)["move"]
        new = self._lookup(sym, move["cid"], move["sent_ms"], stop=True)
        if new is not None:
            self._adopt_stop(pos, str(new["id"]), now_ms)
            return
        old = self._fetch(move["from"], sym, stop=True)
        if old.get("status") == "open":  # nothing happened: the next check moves it again
            self._set_protection(pos, move=None)
            self.persist(pos)
            return
        legs = self._old_legs_final(pos, move)
        if self._executed_during_move(pos, move, legs, now_ms):  # it sold meanwhile
            return
        self._unprotected_after_move(pos, move, "the old protection was cancelled and no new stop was placed", now_ms)

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
        """Cancel the position's exchange protection - the single stop or the OCO pair (one leg
        cancels both) - known or only sent, and record whatever it sold."""
        sym = pos.symbol
        prot = self._protection(pos)
        if prot.get("move"):  # a move in flight: know which order protects before cancelling anything
            self._resolve_move(pos, self.clock())
            prot = self._protection(pos)
        if not pos.sl_order_id and pos.sl_client_id:  # sent, outcome never confirmed
            found = self._lookup(sym, pos.sl_client_id, pos.sl_sent_ms, stop=True)
            if found is None:
                self._clear_protection(pos)
                self.persist(pos)
                return
            pos.sl_order_id = str(found["id"])
            if prot.get("tp_client_id") and not prot.get("tp_order_id"):
                tp = self._lookup(sym, prot["tp_client_id"], pos.sl_sent_ms, stop=True)
                if tp is not None:
                    self._set_protection(pos, tp_order_id=str(tp["id"]))
        if not pos.sl_order_id:
            return
        oid, tp_id = pos.sl_order_id, self._protection(pos).get("tp_order_id")
        self._cancel(oid, sym, stop=True)
        legs = []
        for leg_id, kind in ((oid, "stop"), (tp_id, "tp")):
            if not leg_id:
                continue
            try:
                final = self._fetch(str(leg_id), sym, stop=True)
            except ccxt.BaseError as exc:
                raise ExecutionError(f"could not confirm order {leg_id} for {sym} is cancelled ({exc}); not selling") from exc
            if final.get("status") not in FINAL:
                raise ExecutionError(f"order {leg_id} for {sym} is still {final.get('status')}; not selling")
            legs.append((final, kind))
        for final, kind in legs:
            self._apply(pos, final, kind)
        self._clear_protection(pos)
        self.persist(pos)

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        sym = pos.symbol
        pos.closing_reason = reason
        self._resolve_exit_order(pos)

        def executed(kind):
            return any(f["kind"] == kind and f["filled"] > 0 for f in pos.fills.values())

        before = {k: executed(k) for k in ("stop", "tp")}
        stop_reason = self._stop_exit_reason(pos)  # before the release clears what the stop's level was
        self._release_stop(pos)
        if self._is_dust(sym, pos.open_amount, price):  # the exchange got there first and sold everything
            if executed("tp") and not before["tp"]:
                reason = "take_profit"
            elif executed("stop") and not before["stop"]:
                reason = stop_reason
        if not self._is_dust(sym, pos.open_amount, price):
            self._sell(pos, price)
        if not self._is_dust(sym, pos.open_amount, price):
            raise ExecutionError(f"{pos.open_amount:g} {self._base(sym)} still held after selling; will retry")
        exit_price = pos.exit_value / pos.exit_filled if pos.exit_filled else price
        return finalize_close(pos, exit_price, 0.0, reason, now_ms)

    def _sell(self, pos: Position, price: float) -> None:
        sym, base = pos.symbol, self._base(pos.symbol)
        held = self._satellite_coins(base, self._balance().get("free") or {})  # never the core's coins
        qty = self._amount(sym, min(pos.open_amount, held))
        if self._is_dust(sym, qty, price):
            raise ExecutionError(
                f"the bot expects {pos.open_amount:g} {base} but only {held:g} is free - sold outside the bot? "
                f"Not selling. Check the account, then /forget {pos.id}"
            )
        pos.exit_client_id, pos.exit_sent_ms = self.client_id("x"), self.clock()
        self.persist(pos)
        try:
            order = self.ex.create_order(sym, "market", "sell", qty, None, {"clientOrderId": pos.exit_client_id})
        except ccxt.OperationFailed as exc:  # outcome unknown: resolved by its client id before any retry
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
            held = self._satellite_coins(base, totals)
            prot = self._protection(pos)
            if prot.get("move"):  # a stop move whose answer was lost: sync_stop settles it first
                continue
            if pos.sl_order_id and not pos.closing_reason:
                try:
                    o = self._fetch(pos.sl_order_id, sym, stop=True)
                    t = self._fetch(prot["tp_order_id"], sym, stop=True) if prot.get("tp_order_id") else None
                except ccxt.BaseError as exc:
                    issues.append(SyncIssue(pos, "error", f"could not check the exchange stop: {exc}"))
                    continue
                # an OCO: the leg that executed expires the other, so the take-profit is read first
                done = next(((leg, kind) for leg, kind in ((t, "tp"), (o, "stop"))
                             if leg is not None and float(leg.get("filled") or 0.0) > 0 and leg.get("status") in FINAL),
                            None)
                if done is not None:
                    leg, kind = done
                    filled = float(leg.get("filled") or 0.0)
                    already = float((pos.fills.get(str(leg.get("id"))) or {}).get("filled") or 0.0)
                    left = pos.open_amount - (filled - already)  # a fill recorded earlier is not sold twice
                    # order says sold, coins say not: against the coins there were once protected (other coins
                    # of the currency on the account - a testnet's starting balance, dust - don't count)
                    expect = (float(prot["held"]) - (filled - already) if prot.get("held") is not None else left)
                    if held > expect + max(0.02 * pos.open_amount, 1e-12):
                        issues.append(SyncIssue(pos, "error", f"order {leg.get('id')} reports {filled:g} sold "
                                                               f"but the account still holds {held:g} {base}"))
                        continue
                    reason = "take_profit" if kind == "tp" else self._stop_exit_reason(pos)
                    self._apply(pos, leg, kind)
                    self._clear_protection(pos)
                    if self._is_dust(sym, pos.open_amount, pos.stop_loss):
                        finalize_close(pos, pos.exit_value / pos.exit_filled, 0.0, reason, now_ms)
                    else:
                        pos.closing_reason = reason  # sell what the exchange order left
                    continue
                if o.get("status") in FINAL and (t is None or t.get("status") in FINAL):  # gone, nothing sold
                    status = o.get("status")
                    self._clear_protection(pos)
                    try:
                        self.protect(pos, now_ms)
                    except ProtectionError as exc:
                        issues.append(SyncIssue(pos, "unprotected", f"exchange stop was {status} and could not be replaced: {exc}"))
                    continue
                held += float(o.get("filled") or 0.0)  # a stop-limit partly filled and still working is ours
            if held < pos.open_amount * 0.98:
                issues.append(SyncIssue(pos, "mismatch", f"account holds {held:g} {base} but the bot expects {pos.open_amount:g}"))
        return issues

    # ---------------------------------------------------------- core orders
    def core_order(self, symbol: str, side: str, qty: float, client_id: str, sent_ms: int) -> dict | None:
        """A core sleeve market order (its client id saved by the caller before this is called),
        driven to a final state. None: it was definitely never placed or was rejected.
        Raises ExecutionError while its outcome is unknown."""
        try:
            order = self.ex.create_order(symbol, "market", side, qty, None, {"clientOrderId": client_id})
        except ccxt.OperationFailed as exc:  # network, -1006 status unknown: the client id tells
            log.error("core %s %s: %s - looking it up by client id", side, symbol, exc)
            return self.core_lookup(symbol, client_id, sent_ms)
        except ccxt.BaseError as exc:
            log.warning("core %s %s %g rejected: %s", side, symbol, qty, exc)
            return None
        return self._settle(order, symbol)

    def core_lookup(self, symbol: str, client_id: str, sent_ms: int) -> dict | None:
        """The core order with this client id, settled; None if the exchange never received it."""
        try:
            order = self._lookup(symbol, client_id, sent_ms)
        except ccxt.BaseError as exc:
            raise ExecutionError(f"core order {client_id} on {symbol}: outcome unknown ({exc})") from exc
        return None if order is None else self._settle(order, symbol)

    def core_fill(self, symbol: str, order: dict) -> dict:
        """What a settled order filled: quantity, average price, the fee taken in the coin, all fees
        valued in quote, and the part the quote balance paid (a BNB fee is paid in BNB, not quote)."""
        filled = float(order.get("filled") or 0.0)
        avg = float(order.get("average") or 0.0) or (float(order.get("cost") or 0.0) / filled if filled else 0.0)
        if filled > 0 and avg <= 0:
            raise ExecutionError(f"core order {order.get('id')} filled {filled} but reports no price")
        base_fee, fee, paid_in_quote = self._fee_parts(order, self._base(symbol), avg, symbol)
        return {"filled": filled, "avg": avg, "base_fee": base_fee, "fee": fee, "quote_fee": paid_in_quote}

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
                if cid.startswith(BOT_PREFIX) and str(o.get("id")) not in known_ids and cid not in known_ids:
                    self._cancel(str(o["id"]), sym, stop=True)
                    cancelled.append(f"{sym} order {o['id']} ({cid})")
        return cancelled
