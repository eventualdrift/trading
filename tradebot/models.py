"""Core records: a Signal (what to do) and a Position (what was done)."""
from __future__ import annotations

import itertools
import secrets
import time
from dataclasses import dataclass, field


_ID_COUNTER = itertools.count()
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(n: int, width: int = 0) -> str:
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = _B36[r] + out
    return out.rjust(width, "0")


def new_client_id(kind: str) -> str:
    """Unique client order id: 'tb' + kind + base36 ms + per-process counter + random.
    Alphanumeric and 18 chars - valid on every major exchange."""
    ms = int(time.time() * 1000)
    return f"tb{kind}{_b36(ms, 9)}{_b36(next(_ID_COUNTER) % 1296, 2)}{secrets.token_hex(2)}"


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
    # pending: recorded before the entry order is sent · open · closed
    # failed: the entry definitely bought nothing · unknown: entry outcome unclear (check exchange)
    status: str = "open"
    exit_price: float | None = None
    closed_at: int | None = None
    exit_reason: str | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    fees: float = 0.0
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    client_order_id: str | None = None  # entry order's client id - persisted before sending
    sl_client_id: str | None = None  # stop order sent; kept until its outcome is known
    sl_sent_ms: int = 0
    exit_order_id: str | None = None  # a sell in progress - resolved before any new sell
    exit_client_id: str | None = None
    exit_sent_ms: int = 0
    breakeven_moved: bool = False
    last_checked_ms: int = 0
    exit_filled: float = 0.0  # amount already sold (partial exits)
    exit_value: float = 0.0  # quote proceeds of those sales
    closing_reason: str | None = None  # a close was started but not completed; retried
    fills: dict = field(default_factory=dict, repr=False)  # live: order id -> recorded fill
    features: dict = field(default_factory=dict, repr=False)
    id: int | None = None

    @classmethod
    def from_signal(cls, sig: Signal, mode: str, amount: float, now_ms: int) -> "Position":
        """A pending position, persisted *before* any order is sent."""
        return cls(
            symbol=sig.symbol, timeframe=sig.timeframe, strategy=sig.strategy, side=sig.side,
            mode=mode, amount=amount, entry_price=sig.entry, stop_loss=sig.stop_loss,
            take_profit=sig.take_profit, initial_stop=sig.stop_loss, opened_at=now_ms,
            max_hold_until=sig.max_hold_until, signal_id=sig.id, confidence=sig.confidence,
            status="pending", features=dict(sig.features), client_order_id=new_client_id("e"),
        )

    @property
    def open_amount(self) -> float:
        return max(self.amount - self.exit_filled, 0.0)

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "long" else -1.0

    @property
    def notional(self) -> float:
        return self.entry_price * self.open_amount

    @property
    def initial_risk(self) -> float:
        return abs(self.entry_price - self.initial_stop) * self.amount

    def unrealized(self, price: float) -> float:
        return self.sign * (price - self.entry_price) * self.open_amount

    def r_at(self, price: float) -> float:
        per_unit = abs(self.entry_price - self.initial_stop)
        return self.sign * (price - self.entry_price) / per_unit if per_unit > 0 else 0.0
