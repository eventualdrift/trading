"""Paper trading: simulated fills with fees and slippage, persisted in the DB."""
from __future__ import annotations

from ..backtest.engine import Costs
from ..db import Database
from ..models import Position, Signal
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

    def open_position(self, signal: Signal, amount: float, price: float, now_ms: int) -> Position:
        fill = price * (1 + signal.sign * self.costs.slippage_rate)
        fee = fill * amount * self.costs.fee_rate
        self._add_cash(-fee)
        return Position(
            symbol=signal.symbol, timeframe=signal.timeframe, strategy=signal.strategy,
            side=signal.side, mode=self.mode, amount=amount, entry_price=fill,
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            initial_stop=signal.stop_loss, opened_at=now_ms, max_hold_until=signal.max_hold_until,
            signal_id=signal.id, confidence=signal.confidence, fees=fee,
            features=dict(signal.features),
        )

    def close_position(self, pos: Position, price: float, reason: str, now_ms: int, limit_fill: bool = False) -> Position:
        fill = price if limit_fill else price * (1 - pos.sign * self.costs.slippage_rate)
        fee = fill * pos.amount * self.costs.fee_rate
        self._add_cash(pos.sign * (fill - pos.entry_price) * pos.amount - fee)
        return finalize_close(pos, fill, fee, reason, now_ms)

    def limits(self, symbol: str) -> dict:
        return self.market.limits(symbol) if self.market is not None and hasattr(self.market, "limits") else {}

    def to_precision(self, symbol: str):
        if self.market is not None and hasattr(self.market, "amount_to_precision"):
            return lambda a: self.market.amount_to_precision(symbol, a)
        return None
