"""The core sleeve: a long-only BTC+ETH trend-ensemble allocation (paper only for now).

Each coin gets an equal slot of the core sleeve's equity. At every daily close its
weight is the share of its 50/100/150/200-day averages that the close is above -
0%, 25%, 50%, 75% or 100% of the slot. A coin is only traded when its holding is off
target by more than ``drift_tolerance`` of a slot (a 25% weight step always is) and
by at least ``min_trade_usd``, so it moves roughly 40 times a year per coin rather
than every day. Fills pay the taker fee plus slippage, like any market order.

The sleeve has its own cash and holdings, so its P&L is tracked separately from the
signal strategies ("satellite"), and it compounds on its own equity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .backtest.engine import Costs
from .config import CoreConfig
from .timeframes import drop_unclosed

log = logging.getLogger(__name__)


def trend_weight(closes: pd.Series, sma_days: list[int]) -> float | None:
    """Share of the moving averages the latest close is above; None without enough history."""
    closes = closes.dropna()
    if len(closes) < max(sma_days):
        return None
    last = float(closes.iloc[-1])
    return sum(last > float(closes.iloc[-d:].mean()) for d in sma_days) / len(sma_days)


def trend_weights_series(closes: pd.Series, sma_days: list[int]) -> pd.Series:
    """Vectorised trend_weight for every day (NaN until the longest average exists)."""
    above = sum((closes > closes.rolling(d, min_periods=d).mean()).astype(float) for d in sma_days)
    longest = max(sma_days)
    return (above / len(sma_days)).where(closes.rolling(longest, min_periods=longest).mean().notna())


@dataclass
class CoreTrade:
    symbol: str
    side: str
    qty: float
    price: float
    fee: float
    weight_from: float
    weight_to: float


class CoreSleeve:
    def __init__(self, db, cfg: CoreConfig, costs: Costs, mode: str, market):
        self.db, self.cfg, self.costs, self.mode, self.market = db, cfg, costs, mode, market

    # ------------------------------------------------------------- state
    def _k(self, key: str) -> str:
        return f"{self.mode}:core:{key}"

    def _get(self, key, default=None):
        return self.db.kv_get(self._k(key), default)

    def _set(self, key, value) -> None:
        self.db.kv_set(self._k(key), value)

    @property
    def initialized(self) -> bool:
        return bool(self._get("initialized", False))

    @property
    def cash(self) -> float:
        return float(self._get("cash", 0.0))

    @property
    def holdings(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self._get("holdings", {}) or {}).items()}

    @property
    def weights(self) -> dict[str, float]:
        return dict(self._get("weights", {}) or {})

    @property
    def contributed(self) -> float:
        """Net capital moved into the core sleeve (its P&L = equity - contributed)."""
        return float(self._get("contributed", 0.0))

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(q * prices.get(s, 0.0) for s, q in self.holdings.items())

    # --------------------------------------------------------- capital moves
    def initialize(self, amount: float) -> None:
        self._set("cash", float(amount))
        self._set("contributed", float(amount))
        self._set("holdings", {})
        self._set("initialized", True)

    def deposit(self, amount: float) -> None:
        self._set("cash", self.cash + amount)
        self._set("contributed", self.contributed + amount)

    def withdraw(self, amount: float, prices: dict[str, float], now_ms: int) -> tuple[float, list[CoreTrade]]:
        """Free ``amount`` of cash (selling holdings pro-rata if needed) and take it out."""
        trades = []
        short = amount - self.cash
        value = sum(q * prices[s] for s, q in self.holdings.items() if prices.get(s))
        if short > 0 and value > 0:
            frac = min(short / value * (1 + self.costs.fee_rate + self.costs.slippage_rate), 1.0)
            for s, q in self.holdings.items():
                if q > 0 and prices.get(s):
                    trades.append(self._trade(s, "sell", q * frac, prices[s], now_ms, "sleeve rebalance"))
        out = min(amount, self.cash)
        self._set("cash", self.cash - out)
        self._set("contributed", self.contributed - out)
        return out, trades

    # ------------------------------------------------------------- trading
    def _trade(self, symbol: str, side: str, qty: float, price: float, now_ms: int, reason: str,
               w_from: float = 0.0, w_to: float = 0.0) -> CoreTrade:
        prec = getattr(self.market, "amount_to_precision", None)
        if callable(prec):
            qty = prec(symbol, qty)
        sign = 1 if side == "buy" else -1
        fill = price * (1 + sign * self.costs.slippage_rate)
        fee = fill * qty * self.costs.fee_rate
        holdings = self.holdings
        if side == "buy":
            self._set("cash", self.cash - fill * qty - fee)
            holdings[symbol] = holdings.get(symbol, 0.0) + qty
        else:
            qty = min(qty, holdings.get(symbol, 0.0))
            self._set("cash", self.cash + fill * qty - fee)
            holdings[symbol] = holdings.get(symbol, 0.0) - qty
        self._set("holdings", {k: v for k, v in holdings.items() if v > 1e-12})
        t = CoreTrade(symbol, side, qty, fill, fee, w_from, w_to)
        self.db.insert_core_trade(now_ms, self.mode, t, reason)
        return t

    def target_weights(self, now_ms: int) -> dict[str, float | None]:
        out = {}
        for sym in self.cfg.symbols:
            try:
                d = drop_unclosed(self.market.fetch_ohlcv_df(sym, "1d", limit=max(self.cfg.sma_days) + 20), "1d", now_ms)
                out[sym] = trend_weight(d["close"], self.cfg.sma_days)
            except Exception as exc:
                log.warning("core: no daily data for %s: %s", sym, exc)
                out[sym] = None
        return out

    def rebalance(self, now_ms: int, prices: dict[str, float],
                  weights: dict[str, float | None] | None = None) -> list[CoreTrade]:
        """Move holdings toward target weights; coins without a weight/price are left alone."""
        weights = self.target_weights(now_ms) if weights is None else weights
        eq = self.equity(prices)
        slot = eq / max(len(self.cfg.symbols), 1)
        threshold = max(self.cfg.min_trade_usd, self.cfg.drift_tolerance * slot)
        previous = self.weights
        plans = []
        for sym in self.cfg.symbols:
            w, px = weights.get(sym), prices.get(sym)
            if w is None or not px:
                continue
            delta = slot * w - self.holdings.get(sym, 0.0) * px
            if abs(delta) >= threshold:
                plans.append((sym, delta, px, previous.get(sym, 0.0), w))
        trades = []
        for sym, delta, px, w0, w1 in sorted(plans, key=lambda p: p[1]):  # sells first free the cash
            if delta < 0:
                trades.append(self._trade(sym, "sell", -delta / px, px, now_ms, "trend weights", w0, w1))
            else:
                budget = self.cash / ((1 + self.costs.fee_rate) * (1 + self.costs.slippage_rate))
                qty = min(delta, budget) / px
                if qty * px >= self.cfg.min_trade_usd:
                    trades.append(self._trade(sym, "buy", qty, px, now_ms, "trend weights", w0, w1))
        self._set("weights", {**previous, **{s: w for s, w in weights.items() if w is not None}})
        return trades


def simulate_core(daily_closes: dict[str, pd.Series], cfg: CoreConfig, costs: Costs,
                  start_equity: float = 1000.0) -> pd.Series:
    """Backtest the core sleeve day by day with exactly the live rules -> daily equity."""
    frame = pd.DataFrame(daily_closes).dropna(how="all").sort_index()
    weights = {s: trend_weights_series(frame[s].dropna(), cfg.sma_days).reindex(frame.index) for s in frame}
    n = max(len(frame.columns), 1)
    cash, qty = float(start_equity), {s: 0.0 for s in frame}
    out = []
    buy_cost = (1 + costs.fee_rate) * (1 + costs.slippage_rate)
    for day, row in frame.iterrows():
        prices = {s: float(p) for s, p in row.items() if p == p}
        eq = cash + sum(qty[s] * prices.get(s, 0.0) for s in qty)
        slot = eq / n
        threshold = max(cfg.min_trade_usd, cfg.drift_tolerance * slot)
        plans = []
        for s in frame:
            w = weights[s].get(day)
            if w is None or w != w or s not in prices:
                continue
            delta = slot * w - qty[s] * prices[s]
            if abs(delta) >= threshold:
                plans.append((delta, s))
        for delta, s in sorted(plans):
            px = prices[s]
            if delta < 0:
                q = min(-delta / px, qty[s])
                fill = px * (1 - costs.slippage_rate)
                cash += fill * q * (1 - costs.fee_rate)
                qty[s] -= q
            else:
                spend = min(delta, cash / buy_cost)
                fill = px * (1 + costs.slippage_rate)
                q = spend / px
                cash -= fill * q * (1 + costs.fee_rate)
                qty[s] += q
        out.append(cash + sum(qty[s] * prices.get(s, 0.0) for s in qty))
    return pd.Series(np.array(out), index=frame.index, dtype=float)
