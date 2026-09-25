"""Core records: a Signal (what to do) and a Position (what was done)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    timeframe: str
    strategy: str
    side: str  # "long" | "short"
    entry: float  # reference price (close of the signal candle)
    stop_loss: float
    take_profit: float
    candle_time: int  # open time (ms) of the candle that produced the signal
    created_at: int  # ms
    valid_until: int  # ms - don't enter after the next candle closes
    max_hold_until: int  # ms - time stop
    reason: str = ""
    confidence: float | None = None  # ML probability of a winning trade
    expected_r: float | None = None
    status: str = "new"  # new | opened | skipped | filtered
    note: str = ""
    features: dict = field(default_factory=dict, repr=False)
    id: int | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def reward_risk(self) -> float:
        r = self.risk_per_unit
        return abs(self.take_profit - self.entry) / r if r > 0 else 0.0

    @property
    def sl_pct(self) -> float:
        return (self.stop_loss / self.entry - 1.0) * 100.0

    @property
    def tp_pct(self) -> float:
        return (self.take_profit / self.entry - 1.0) * 100.0

    def chase_limit(self, max_chase_r: float) -> float:
        """Don't enter beyond this price - the move has already happened."""
        return self.entry + self.sign * max_chase_r * self.risk_per_unit


@dataclass
class Position:
    symbol: str
    timeframe: str
    strategy: str
    side: str
    mode: str  # paper | live
    amount: float
    entry_price: float
    stop_loss: float
    take_profit: float
    initial_stop: float
    opened_at: int
    max_hold_until: int
    signal_id: int | None = None
    confidence: float | None = None
    status: str = "open"  # open | closed
    exit_price: float | None = None
    closed_at: int | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    fees: float = 0.0
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    breakeven_moved: bool = False
    last_checked_ms: int = 0
    features: dict = field(default_factory=dict, repr=False)
    id: int | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    @property
    def notional(self) -> float:
        return self.entry_price * self.amount

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.initial_stop) * self.amount

    def unrealized(self, price: float) -> float:
        return self.sign * (price - self.entry_price) * self.amount

    def r_at(self, price: float) -> float:
        per_unit = abs(self.entry_price - self.initial_stop)
        return self.sign * (price - self.entry_price) / per_unit if per_unit > 0 else 0.0
