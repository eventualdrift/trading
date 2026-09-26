"""Paper trading: simulated fills with fees and slippage, persisted in the DB.

Fills mirror what the live broker does: entries and exits are market orders at
the current price plus slippage. The one exception is the protective stop-loss,
which live trading keeps on the exchange; the bot passes its trigger level (or
the gap price) here, like a stop-market order would fill.
"""
from __future__ import annotations

from ..backtest.engine import Costs
from ..db import Database
from ..models import Position
from .base import Broker, finalize_close


class PaperBroker(Broker):
    mode = "paper"

    def __init__(self, db: Database, costs: Costs, starting_balance: float = 1000.0, market=None):
        self.db = db
        self.costs = costs
        self.market = market
        if db.kv_get("paper_cash") is None:
            db.kv_set("paper_cash", float(starting_balance))
            db.kv_set("paper_starting_balance", float(starting_balance))

    @property
    def cash(self) -> float:
        return float(self.db.kv_get("paper_cash", 0.0))

    def _add_cash(self, delta: float) -> None:
        self.db.kv_set("paper_cash", self.cash + delta)

    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        # Margin-style accounting: cash holds realised P&L; open trades add unrealised P&L.
        return self.cash + sum(p.unrealized(prices.get(p.symbol, p.entry_price)) for p in positions)

    def open_position(self, pos: Position, price: float, now_ms: int) -> Position:
        fill = price * (1 + pos.sign * self.costs.slippage_rate)
        fee = fill * pos.amount * self.costs.fee_rate
        self._add_cash(-fee)
        pos.entry_price = fill
        pos.fees = fee
        pos.opened_at = now_ms
        pos.status = "open"
        return pos

    def transfer(self, delta: float) -> None:
        """Move cash in (+) or out (-) of the satellite sleeve (core/satellite rebalancing)."""
        self._add_cash(delta)

    def fill_limit(self, pos: Position, now_ms: int) -> Position:
        """A resting limit entry got filled at its price: maker fee, no slippage."""
        fee = pos.entry_price * pos.amount * self.costs.entry_fee
        self._add_cash(-fee)
        pos.fees = fee
        pos.opened_at = now_ms
        pos.status = "open"
        return pos

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        fill = price * (1 - pos.sign * self.costs.slippage_rate)
        fee = fill * pos.amount * self.costs.fee_rate
        self._add_cash(pos.sign * (fill - pos.entry_price) * pos.amount - fee)
        return finalize_close(pos, fill, fee, reason, now_ms)

    def limits(self, symbol: str) -> dict:
        return self.market.limits(symbol) if self.market is not None and hasattr(self.market, "limits") else {}

    def to_precision(self, symbol: str):
        if self.market is not None and hasattr(self.market, "amount_to_precision"):
            return lambda a: self.market.amount_to_precision(symbol, a)
        return None
