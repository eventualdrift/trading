from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..models import Position


class NotFilled(RuntimeError):
    """The entry order definitely bought nothing - safe to forget the trade."""


class ProtectionError(RuntimeError):
    """The position could not be protected by an exchange-side stop-loss."""


class ExecutionError(RuntimeError):
    """An order action failed or its outcome is uncertain; state needs attention."""


@dataclass
class SyncIssue:
    position: Position
    kind: str  # unprotected | mismatch | error
    message: str


class Broker(ABC):
    mode: str = "base"

    @abstractmethod
    def equity(self, prices: dict[str, float], positions: list[Position]) -> float:
        """Account value in quote currency, marking open positions at ``prices``."""

    def available_cash(self) -> float | None:
        """Free quote balance for new longs (None = only exposure limits apply)."""
        return None

    @abstractmethod
    def open_position(self, pos: Position, price: float, now_ms: int) -> Position:
        """Fill the pending ``pos`` (entry price, amount, fees, status='open').
        Raises NotFilled if nothing was bought; any other exception means the
        outcome is uncertain."""

    def protect(self, pos: Position, now_ms: int) -> None:
        """Put exchange-side protection in place. Raises ProtectionError if the
        position is unprotected. May close ``pos`` if the stop executed at once."""

    @abstractmethod
    def close_position(self, pos: Position, price: float, reason: str, now_ms: int) -> Position:
        """Exit ``pos`` at market. ``price`` is the current (or stop-trigger) price.
        Raises ExecutionError if the exit is incomplete; the caller retries."""

    def move_stop(self, pos: Position, new_stop: float) -> None:
        pos.stop_loss = new_stop

    def sync(self, positions: list[Position], now_ms: int) -> list[SyncIssue]:
        """Reconcile with the exchange: record stops that filled (closing those
        positions in place) and report anything that needs attention."""
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
    pos.closing_reason = None
    pos.closed_at = now_ms
    pos.status = "closed"
    return pos
