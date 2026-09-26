"""Strategy interface.

A strategy turns an OHLCV frame into per-bar entry/exit flags plus the stop-loss
and take-profit level each entry would use. Everything is vectorised and causal:
row t only depends on rows <= t, so the same code drives backtests and live scans.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np
import pandas as pd

SIGNAL_COLUMNS = [
    "enter_long",
    "enter_short",
    "long_sl",
    "long_tp",
    "short_sl",
    "short_tp",
    "exit_long",
    "exit_short",
]


COMMON_PARAMS = {
    "btc_filter": False,  # only take longs while BTC is in an uptrend (shorts only in a downtrend)
}


class Strategy(ABC):
    name: str = "base"
    description: str = ""
    default_params: dict = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params) - set(COMMON_PARAMS)
        if unknown:
            raise ValueError(f"{self.name}: unknown params {sorted(unknown)}")
        self.params = {**COMMON_PARAMS, **self.default_params, **params}

    @property
    def warmup(self) -> int:
        """Bars needed before the indicators are valid."""
        return 250

    @property
    def max_hold_bars(self) -> int:
        return int(self.params.get("max_hold_bars", 60))

    @abstractmethod
    def _populate(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns and any of SIGNAL_COLUMNS to ``df`` (a copy)."""

    def explain(self, row: pd.Series, side: str) -> str:
        """Human readable reason for an entry on ``row``."""
        return self.description

    def populate(self, df: pd.DataFrame, context=None, tf: str | None = None) -> pd.DataFrame:
        """Indicators + signal columns. ``context`` (a MarketContext) is needed when the
        btc_filter option is on; without it a filtered strategy takes no entries."""
        out = self._populate(df.copy())
        for col in ("enter_long", "enter_short", "exit_long", "exit_short"):
            if col not in out:
                out[col] = False
            out[col] = out[col].fillna(False).astype(bool)
        for col in ("long_sl", "long_tp", "short_sl", "short_tp", "trail_dist"):
            if col not in out:
                out[col] = np.nan
        # An entry is only valid if its levels make sense.
        c = out["close"]
        out["enter_long"] &= (out["long_sl"] < c) & (out["long_tp"] > c)
        out["enter_short"] &= (out["short_sl"] > c) & (out["short_tp"] < c)
        if self.params.get("btc_filter"):
            if context is None or len(out) == 0:
                up = np.full(len(out), np.nan)
            else:
                up = context.align(out.index, tf or infer_tf(out.index))["btc_uptrend"].to_numpy()
            out["btc_uptrend"] = up
            out["enter_long"] &= up == 1.0
            out["enter_short"] &= up == 0.0
        return out

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.params})"


def rr_targets(close: pd.Series, atr: pd.Series, sl_atr: float, rr: float):
    """Symmetric ATR stop and fixed reward:risk target for both sides."""
    risk = sl_atr * atr
    return close - risk, close + rr * risk, close + risk, close - rr * risk


def infer_tf(index: pd.DatetimeIndex) -> str:
    from ..timeframes import TF_SECONDS

    if len(index) < 2:
        raise ValueError("cannot infer the timeframe of fewer than 2 bars; pass tf=")
    step = pd.Series(index[1:] - index[:-1]).median().total_seconds()
    return min(TF_SECONDS, key=lambda k: abs(TF_SECONDS[k] - step))
