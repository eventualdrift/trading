"""Live spot trading through ccxt.

Safety design:
  * long-only spot (no leverage, no liquidation);
  * market entry, then an exchange-side stop-loss order is placed as a safety
    net so the position is protected even if the bot/VPS goes offline;
  * take-profit, breakeven and time exits are managed by the bot (it cancels the
    exchange stop first, then sells);
  * every step is logged; failures raise so the bot can alert you.
"""
from __future__ import annotations

import logging
import time

import ccxt

from ..data.exchange import ExchangeClient, with_retries
from ..models import Position, Signal
from .base import Broker, finalize_close

log = logging.getLogger(__name__)


class LiveBroker(Broker):
    mode = "live"

    def __init__(self, client: ExchangeClient, quote: str, native_stop_loss: bool = True):
        self.client = client
        self.ex = client.ex
        self.quote = quote
        self.native_stop_loss = native_stop_loss

    # ------------------------------------------------------------ balances
    def _balance(self) -> dict:
        return with_retries(self.ex.fetch_balance)

    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        bal = self._balance()
        quote_total = float((bal.get("total") or {}).get(self.quote) or 0.0)
        return quote_total + sum(p.amount * prices.get(p.symbol, p.entry_price) for p in positions)

    def available_cash(self) -> float | None:
        return float((self._balance().get("free") or {}).get(self.quote) or 0.0)

    def limits(self, symbol: str) -> dict:
        return self.client.limits(symbol)

    def to_precision(self, symbol: str):
        return lambda a: self.client.amount_to_precision(symbol, a)

    # -------------------------------------------------------------- orders
    def _wait_filled(self, order: dict, symbol: str, timeout_s: float = 20.0) -> dict:
        deadline = time.time() + timeout_s
        while order.get("status") not in ("closed", "canceled", "expired", "rejected") and time.time() < deadline:
            time.sleep(1.0)
            order = with_retries(self.ex.fetch_order, order["id"], symbol)
        return order

    @staticmethod
    def _fees(order: dict) -> list[dict]:
        fees = list(order.get("fees") or [])
        if not fees and order.get("fee"):
            fees = [order["fee"]]
        return [f for f in fees if f and f.get("cost")]

    def _place_stop(self, symbol: str, amount: float, stop: float) -> str | None:
        if not self.native_stop_loss:
            return None
        stop_p = self.client.price_to_precision(symbol, stop)
        attempts = [
            ("market", None),
            ("limit", self.client.price_to_precision(symbol, stop * 0.995)),  # stop-limit fallback
        ]
        for otype, limit_price in attempts:
            try:
                order = self.ex.create_order(symbol, otype, "sell", amount, limit_price, {"stopLossPrice": stop_p})
                log.info("placed %s stop-loss for %s %s @ %s (order %s)", otype, amount, symbol, stop_p, order["id"])
                return str(order["id"])
            except (ccxt.NotSupported, ccxt.InvalidOrder, ccxt.BadRequest, ccxt.ExchangeError) as exc:
                log.warning("stop-loss %s order failed on %s: %s", otype, self.client.id, exc)
        log.error("no exchange-side stop for %s - the bot will manage the stop itself", symbol)
        return None

    def _cancel(self, order_id: str | None, symbol: str) -> dict | None:
        """Cancel an order; returns the order if it had already filled."""
        if not order_id:
            return None
        try:
            order = with_retries(self.ex.fetch_order, order_id, symbol)
            if order.get("status") == "closed":
                return order
            with_retries(self.ex.cancel_order, order_id, symbol)
        except ccxt.OrderNotFound:
            pass
        return None

    def open_position(self, signal: Signal, amount: float, price: float, now_ms: int) -> Position:
        if signal.side != "long":
            raise ValueError("Live spot trading is long-only")
        sym = signal.symbol
        amount = self.client.amount_to_precision(sym, amount)
        order = self.ex.create_order(sym, "market", "buy", amount)  # not retried: avoid double buys
        order = self._wait_filled(order, sym)
        filled = float(order.get("filled") or 0.0)
        if filled <= 0:
            raise RuntimeError(f"entry order for {sym} did not fill: {order.get('status')}")
        avg = float(order.get("average") or order.get("price") or price)
        base = self.client.market(sym)["base"]
        fee_quote, net_amount = 0.0, filled
        for f in self._fees(order):
            if f.get("currency") == base:
                net_amount -= float(f["cost"])  # fee taken from the coins received
            elif f.get("currency") == self.quote:
                fee_quote += float(f["cost"])
        net_amount = self.client.amount_to_precision(sym, net_amount)
        pos = Position(
            symbol=sym, timeframe=signal.timeframe, strategy=signal.strategy, side="long",
            mode=self.mode, amount=net_amount, entry_price=avg, stop_loss=signal.stop_loss,
            take_profit=signal.take_profit, initial_stop=signal.stop_loss, opened_at=now_ms,
            max_hold_until=signal.max_hold_until, signal_id=signal.id, confidence=signal.confidence,
            fees=fee_quote, entry_order_id=str(order.get("id")), features=dict(signal.features),
        )
        pos.sl_order_id = self._place_stop(sym, net_amount, signal.stop_loss)
        return pos

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int, limit_fill: bool = False) -> Position:
        filled_stop = self._cancel(pos.sl_order_id, pos.symbol)
        if filled_stop is not None:  # the exchange stop got there first
            return self._close_from_order(pos, filled_stop, "stop_loss", now_ms)
        base = self.client.market(pos.symbol)["base"]
        free_base = float((self._balance().get("free") or {}).get(base) or 0.0)
        amount = self.client.amount_to_precision(pos.symbol, min(pos.amount, free_base))
        if amount <= 0:
            raise RuntimeError(f"nothing to sell for {pos.symbol} (free {base} = {free_base})")
        order = self.ex.create_order(pos.symbol, "market", "sell", amount)
        order = self._wait_filled(order, pos.symbol)
        return self._close_from_order(pos, order, reason, now_ms)

    def _close_from_order(self, pos: Position, order: dict, reason: str, now_ms: int) -> Position:
        avg = float(order.get("average") or order.get("price") or pos.stop_loss)
        fee = 0.0
        for f in self._fees(order):
            cost = float(f["cost"])
            fee += cost if f.get("currency") == self.quote else cost * avg
        return finalize_close(pos, avg, fee, reason, now_ms)

    def move_stop(self, pos: Position, new_stop: float) -> None:
        filled = self._cancel(pos.sl_order_id, pos.symbol)
        if filled is not None:
            return  # already stopped out; sync() will record it
        pos.stop_loss = new_stop
        pos.sl_order_id = self._place_stop(pos.symbol, pos.amount, new_stop)

    def sync(self, positions: list[Position], now_ms: int) -> list[Position]:
        closed = []
        for pos in positions:
            if not pos.sl_order_id:
                continue
            try:
                order = with_retries(self.ex.fetch_order, pos.sl_order_id, pos.symbol)
            except ccxt.OrderNotFound:
                log.warning("stop order %s for %s not found; re-placing", pos.sl_order_id, pos.symbol)
                pos.sl_order_id = self._place_stop(pos.symbol, pos.amount, pos.stop_loss)
                continue
            status = order.get("status")
            if status == "closed":
                closed.append(self._close_from_order(pos, order, "stop_loss", now_ms))
            elif status in ("canceled", "expired", "rejected"):
                log.warning("stop order for %s was %s; re-placing", pos.symbol, status)
                pos.sl_order_id = self._place_stop(pos.symbol, pos.amount, pos.stop_loss)
        return closed
