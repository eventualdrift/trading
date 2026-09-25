from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Position, Signal


class Broker(ABC):
    mode: str = "base"

    @abstractmethod
    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        """Account value in quote currency, marking open positions at ``prices``."""

    def available_cash(self) -> float | None:
        """Free quote balance for new longs (None = only exposure limits apply)."""
        return None

    @abstractmethod
    def open_position(self, signal: Signal, amount: float, price: float, now_ms: int) -> Position:
        ...

    @abstractmethod
    def close_position(self, pos: Position, price: float, reason: str, now_ms: int, limit_fill: bool = False) -> Position:
        """Close ``pos``. ``price`` is the trigger/market price. ``limit_fill`` means a
        take-profit level was reached (limit order semantics: no slippage)."""

    def move_stop(self, pos: Position, new_stop: float) -> None:
        pos.stop_loss = new_stop

    def sync(self, positions: list[Position], now_ms: int) -> list[Position]:
        """Detect positions closed on the exchange side (e.g. a native stop filled)."""
        return []

    def limits(self, symbol: str) -> dict:
        return {}

    def to_precision(self, symbol: str):
        return None


def finalize_close(pos: Position, exit_price: float, exit_fee: float, reason: str, now_ms: int) -> Position:
    pos.exit_price = exit_price
    pos.fees += exit_fee
    pos.pnl = pos.sign * (exit_price - pos.entry_price) * pos.amount - pos.fees
    pos.r_multiple = pos.pnl / pos.initial_risk if pos.initial_risk > 0 else 0.0
    pos.exit_reason = reason
    pos.closed_at = now_ms
    pos.status = "closed"
    return pos
