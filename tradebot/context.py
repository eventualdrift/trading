"""Cross-asset market context from BTC, joined to any strategy's bars without lookahead.

* ``btc_uptrend``: BTC's daily close is above its N-day average (default 200).
* ``btc_vol_ratio``: volatility of BTC's hourly returns over the last 20h divided by
  that of the 100h BEFORE them. Around 1 is normal; 2.5+ is a burst (flash crash,
  news). The windows don't overlap - if they did, the ratio could never exceed
  sqrt(100/20) = 2.24 and a 2.5 threshold would never fire.

A strategy bar that closes at time T only ever sees context bars that had already
closed by T, so backtests, ML labels and the live scanner all see the same values.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .timeframes import tf_ms


@dataclass
class MarketContext:
    daily: pd.DataFrame | None = None  # BTC 1d candles
    hourly: pd.DataFrame | None = None  # BTC 1h candles
    uptrend_days: int = 200
    vol_short: int = 20
    vol_long: int = 100

    def _daily_series(self) -> pd.DataFrame:
        if self.daily is None or len(self.daily) == 0:
            return pd.DataFrame(columns=["btc_uptrend"])
        c = self.daily["close"]
        sma = c.rolling(self.uptrend_days, min_periods=self.uptrend_days).mean()
        up = (c > sma).astype(float).where(sma.notna())  # NaN = not enough history yet
        out = pd.DataFrame({"btc_uptrend": up.to_numpy()},
                           index=self.daily.index + pd.Timedelta(milliseconds=tf_ms("1d")))
        return out

    def _hourly_series(self) -> pd.DataFrame:
        if self.hourly is None or len(self.hourly) == 0:
            return pd.DataFrame(columns=["btc_vol_ratio"])
        lr = np.log(self.hourly["close"]).diff()
        ratio = lr.rolling(self.vol_short).std() / lr.shift(self.vol_short).rolling(self.vol_long).std()
        return pd.DataFrame({"btc_vol_ratio": ratio.to_numpy()},
                            index=self.hourly.index + pd.Timedelta(milliseconds=tf_ms("1h")))

    def align(self, index: pd.DatetimeIndex, tf: str) -> pd.DataFrame:
        """Context values known at the close of each bar (bars given by their open times)."""
        close_t = (index + pd.Timedelta(milliseconds=tf_ms(tf))).as_unit("ns")
        left = pd.DataFrame({"t": close_t}).reset_index(drop=True)
        out = pd.DataFrame(index=index)
        for series in (self._daily_series(), self._hourly_series()):
            col = series.columns[0]
            if series.empty:
                out[col] = np.nan
                continue
            right = pd.DataFrame({"ct": series.index.as_unit("ns"), col: series[col].to_numpy()})
            merged = pd.merge_asof(left, right, left_on="t", right_on="ct", direction="backward")
            out[col] = merged[col].to_numpy()
        return out

    def uptrend_now(self) -> bool | None:
        s = self._daily_series()["btc_uptrend"].dropna()
        return None if s.empty else bool(s.iloc[-1])

    def vol_ratio_now(self) -> float | None:
        s = self._hourly_series()["btc_vol_ratio"].dropna()
        return None if s.empty else float(s.iloc[-1])


def context_symbol(quote: str) -> str:
    return f"BTC/{quote}"


def apply_vol_breaker(pop: pd.DataFrame, context: "MarketContext | None", tf: str, ratio: float | None) -> pd.DataFrame:
    """Block entries on bars where BTC's volatility ratio exceeds ``ratio`` (backtests)."""
    if context is None or ratio is None or len(pop) == 0:
        return pop
    vr = context.align(pop.index, tf)["btc_vol_ratio"].to_numpy()
    burst = np.nan_to_num(vr, nan=0.0) > ratio
    pop["enter_long"] &= ~burst
    pop["enter_short"] &= ~burst
    return pop
