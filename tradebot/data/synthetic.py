"""Synthetic markets for offline demos and tests.

Prices follow a regime-switching random walk (up-trends, down-trends and ranges
with volatility clustering). Useful to prove the plumbing works end-to-end.
Results on synthetic data say NOTHING about real-market profitability.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..timeframes import index_ms, resample_ohlcv, tf_ms, tf_seconds


def generate_ohlcv(
    n_bars: int,
    tf: str = "15m",
    seed: int = 0,
    start: str = "2024-01-01",
    start_price: float = 100.0,
    regime_bars: tuple[int, int] = (600, 3000),
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    scale = np.sqrt(tf_seconds(tf) / 900.0)
    sigma = 0.0035 * scale
    rets = np.empty(n_bars)
    vol_state = 0.0
    logp = 0.0
    i = 0
    while i < n_bars:
        seg = int(rng.integers(*regime_bars))
        regime = rng.choice(["up", "down", "range"], p=[0.3, 0.25, 0.45])
        drift = rng.uniform(0.00012, 0.0004) * scale**2
        anchor = logp
        for _ in range(min(seg, n_bars - i)):
            vol_state = 0.97 * vol_state + 0.2 * rng.standard_normal()
            s = sigma * np.exp(0.35 * vol_state)
            if regime == "up":
                r = drift + s * rng.standard_normal()
            elif regime == "down":
                r = -drift + s * rng.standard_normal()
            else:
                r = -0.02 * (logp - anchor) + s * rng.standard_normal()
            rets[i] = r
            logp += r
            i += 1
    close = start_price * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[start_price], close[:-1]]) * np.exp(rng.normal(0, sigma * 0.05, n_bars))
    wick = np.abs(rng.normal(0, sigma * 0.6, (2, n_bars)))
    high = np.maximum(open_, close) * np.exp(wick[0])
    low = np.minimum(open_, close) * np.exp(-wick[1])
    volume = np.exp(rng.normal(10, 0.3, n_bars)) * (1 + 3 * np.abs(rets) / sigma)
    idx = pd.date_range(start, periods=n_bars, freq=f"{tf_seconds(tf)}s", tz="UTC", name="time")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


class SyntheticMarket:
    """Drop-in replacement for ExchangeClient's market-data methods, with a movable clock."""

    id = "synthetic"

    def __init__(
        self,
        symbols: list[str] | int = 6,
        days: int = 365,
        base_tf: str = "15m",
        seed: int = 42,
        start: str = "2024-01-01",
    ):
        if isinstance(symbols, int):
            names = ["BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "TRX"]
            symbols = [f"{names[i % len(names)]}{'' if i < len(names) else i}/USDT" for i in range(symbols)]
        self.symbols = list(symbols)
        self.base_tf = base_tf
        self.price_bar_ms = tf_ms(base_tf)
        n = int(days * 86400 / tf_seconds(base_tf))
        self.base = {
            s: generate_ohlcv(n, base_tf, seed=seed * 1000 + k, start=start, start_price=float(10 ** (1 + k % 4)))
            for k, s in enumerate(self.symbols)
        }
        self._frames: dict[tuple[str, str], pd.DataFrame] = {}
        last = self.base[self.symbols[0]].index[-1]
        self._now = int(last.value // 1_000_000) + self.price_bar_ms

    # clock -------------------------------------------------------------------
    def now_ms(self) -> int:
        return self._now

    def set_now(self, ms: int) -> None:
        self._now = int(ms)

    @property
    def start_ms(self) -> int:
        return int(self.base[self.symbols[0]].index[0].value // 1_000_000)

    # data --------------------------------------------------------------------
    def _frame(self, symbol: str, tf: str) -> pd.DataFrame:
        key = (symbol, tf)
        if key not in self._frames:
            base = self.base[symbol]
            self._frames[key] = base if tf == self.base_tf else resample_ohlcv(base, tf)
        return self._frames[key]

    def _closed(self, symbol: str, tf: str) -> pd.DataFrame:
        f = self._frame(symbol, tf)
        n = int(np.searchsorted(index_ms(f.index) + tf_ms(tf), self._now, side="right"))
        return f.iloc[:n]

    def fetch_ohlcv_df(self, symbol: str, tf: str, limit: int = 300, since: int | None = None) -> pd.DataFrame:
        return self._closed(symbol, tf).tail(limit)

    def history(self, symbol: str, tf: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
        f = self._closed(symbol, tf)
        ms = index_ms(f.index)
        mask = ms >= start_ms
        if end_ms is not None:
            mask &= ms < end_ms
        return f[mask]

    def fetch_price_bars(self, symbol: str, since_ms: int) -> pd.DataFrame:
        f = self._closed(symbol, self.base_tf)
        return f[index_ms(f.index) >= since_ms]

    def fetch_last_price(self, symbol: str) -> float:
        return float(self._closed(symbol, self.base_tf)["close"].iloc[-1])

    def fetch_last_price_ts(self, symbol: str) -> tuple[float, int]:
        return self.fetch_last_price(symbol), self._now

    def top_symbols(self, quote: str, n: int, min_quote_volume: float = 0.0,
                    whitelist: list[str] | None = None, blacklist: list[str] | None = None) -> list[str]:
        syms = [s for s in (whitelist or self.symbols) if s in self.base]
        return [s for s in syms if s not in (blacklist or [])][:n]

    def limits(self, symbol: str) -> dict:
        return {"min_amount": 0.0, "min_cost": 5.0}

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(f"{amount:.8f}")

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(f"{price:.8g}")
