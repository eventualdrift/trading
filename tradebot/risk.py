"""Risk management: position sizing and circuit breakers.

The goal is survival first. A 1% risk per trade means ten losses in a row cost
about 10% of the account, not the account.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import RiskConfig
from .models import Position, Signal


def position_fraction(stop_pct: float, cfg: RiskConfig) -> float:
    """Position size as a fraction of equity: risk / stop distance, capped per position."""
    return min(cfg.risk_per_trade_pct / 100.0 / max(stop_pct, 1e-9), cfg.max_position_pct / 100.0)


def risk_at_stake(side: str, entry: float, stop: float, amount: float) -> float:
    """Money lost if the current stop is hit (before costs); 0 once the stop is at or past entry."""
    per_unit = (entry - stop) if side == "long" else (stop - entry)
    return max(per_unit, 0.0) * amount


@dataclass
class SizeDecision:
    amount: float
    notional: float
    risk_amount: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.amount > 0


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def size(
        self,
        equity: float,
        entry: float,
        stop: float,
        open_notional: float = 0.0,
        available_cash: float | None = None,
        limits: dict | None = None,
        to_precision: Callable[[float], float] | None = None,
        risk_multiplier: float = 1.0,
    ) -> SizeDecision:
        c = self.cfg
        per_unit = abs(entry - stop)
        if equity <= 0 or entry <= 0 or per_unit <= 0:
            return SizeDecision(0, 0, 0, "invalid equity/entry/stop")
        mult = min(max(risk_multiplier, 1.0), c.max_risk_multiplier)
        risk_budget = equity * c.risk_per_trade_pct * mult / 100.0
        notional = risk_budget / per_unit * entry
        caps = {
            "max position size": equity * c.max_position_pct / 100.0,
            "total exposure limit": equity * c.max_total_exposure_pct / 100.0 - open_notional,
        }
        if available_cash is not None:
            caps["available balance"] = available_cash * 0.98  # leave room for fees
        for name, cap in caps.items():
            if cap <= 0:
                return SizeDecision(0, 0, 0, f"{name} reached")
            notional = min(notional, cap)
        amount = notional / entry
        if to_precision:
            amount = to_precision(amount)
        notional = amount * entry
        limits = limits or {}
        min_amount, min_cost = limits.get("min_amount") or 0.0, limits.get("min_cost") or 0.0
        if amount <= 0 or amount < min_amount or notional < min_cost:
            return SizeDecision(
                0, 0, 0,
                f"position ${notional:,.2f} is below the exchange minimum "
                f"(min cost ${min_cost:,.2f}, min amount {min_amount}) - add funds or raise risk",
            )
        return SizeDecision(amount, notional, amount * per_unit)

    def entry_block_reason(self, signal: Signal, open_positions: list[Position],
                           equity: float | None = None) -> str | None:
        budget = self.cfg.max_open_risk_pct
        if budget is None:
            if len(open_positions) >= self.cfg.max_open_positions:
                return f"max open positions ({self.cfg.max_open_positions}) reached"
        elif equity is not None and equity > 0:
            at_stake = sum(risk_at_stake(p.side, p.entry_price, p.stop_loss, p.open_amount) for p in open_positions)
            stop_pct = abs(signal.entry - signal.stop_loss) / signal.entry if signal.entry > 0 else 1.0
            new = position_fraction(stop_pct, self.cfg) * stop_pct * equity
            if at_stake + new > equity * budget / 100.0 + 1e-9:
                return (f"open-risk budget ({budget:g}%) reached: {at_stake / equity:.1%} at stake "
                        f"+ {new / equity:.1%} for this trade")
        if any(p.symbol == signal.symbol for p in open_positions):
            return f"already in a {signal.symbol} position"
        if signal.reward_risk < self.cfg.min_reward_risk - 1e-9:
            return f"reward:risk {signal.reward_risk:.2f} below {self.cfg.min_reward_risk}"
        return None

    def daily_limit_hit(self, equity: float, day_start_equity: float) -> bool:
        if self.cfg.daily_loss_limit_pct <= 0 or day_start_equity <= 0:
            return False
        return equity <= day_start_equity * (1 - self.cfg.daily_loss_limit_pct / 100.0)

    def drawdown_hit(self, equity: float, peak_equity: float) -> bool:
        if self.cfg.max_drawdown_pct <= 0 or peak_equity <= 0:
            return False
        return equity <= peak_equity * (1 - self.cfg.max_drawdown_pct / 100.0)
