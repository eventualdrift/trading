from __future__ import annotations

import pandas as pd

from .. import indicators as ta
from .base import Strategy, rr_targets


class MeanReversion(Strategy):
    """Fade stretched moves in ranging markets.

    Only trades when ADX says there is no strong trend. Enter when price closes
    back inside the Bollinger band after an oversold (RSI) poke below it.
    Exit early when price is back at the middle band (the mean).
    """

    name = "meanrev"
    description = "Range mean reversion"
    default_params = {
        "bb_len": 20,
        "bb_k": 2.0,
        "rsi_len": 14,
        "rsi_low": 30.0,
        "adx_len": 14,
        "adx_max": 25.0,
        "atr_len": 14,
        "sl_atr": 1.5,
        "rr": 1.5,
        "max_hold_bars": 30,
    }

    @property
    def warmup(self) -> int:
        return 100

    def _populate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        df["bb_mid"], df["bb_upper"], df["bb_lower"] = ta.bollinger(c, p["bb_len"], p["bb_k"])
        df["rsi"] = ta.rsi(c, p["rsi_len"])
        df["adx"], _, _ = ta.adx(df, p["adx_len"])
        df["atr"] = ta.atr(df, p["atr_len"])
        ranging = df["adx"] < p["adx_max"]
        was_oversold = df["rsi"].rolling(3).min() < p["rsi_low"]
        was_overbought = df["rsi"].rolling(3).max() > 100 - p["rsi_low"]
        prev_c = c.shift(1)

        df["enter_long"] = (
            ranging & was_oversold & (prev_c < df["bb_lower"].shift(1)) & (c > df["bb_lower"])
        )
        df["enter_short"] = (
            ranging & was_overbought & (prev_c > df["bb_upper"].shift(1)) & (c < df["bb_upper"])
        )
        df["long_sl"], df["long_tp"], df["short_sl"], df["short_tp"] = rr_targets(
            c, df["atr"], p["sl_atr"], p["rr"]
        )
        df["exit_long"] = c >= df["bb_mid"]
        df["exit_short"] = c <= df["bb_mid"]
        return df

    def explain(self, row: pd.Series, side: str) -> str:
        if side == "long":
            return f"ranging market (ADX {row['adx']:.0f}); oversold dip below lower band reclaimed, RSI {row['rsi']:.0f}"
        return f"ranging market (ADX {row['adx']:.0f}); overbought spike above upper band rejected, RSI {row['rsi']:.0f}"
