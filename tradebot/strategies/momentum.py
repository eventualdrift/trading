from __future__ import annotations

import pandas as pd

from .. import indicators as ta
from .base import Strategy, rr_targets


class MomentumTrend(Strategy):
    """Classic trend following ("let winners run").

    Buys a close above the highest high of the last 55 bars while the long-term
    trend is up and ADX confirms a trending market. There is no near profit
    target: once the trade is +1R the stop moves to breakeven and then trails
    3 ATR below the highest high, so the rare big trend pays for the many small
    losers. Exits early on a close below the 20-bar low. Fewer trades, larger
    winners - the profile of most long-term profitable trend systems.
    """

    name = "momentum"
    description = "Trend following with trailing stop"
    default_params = {
        "entry_len": 55,
        "exit_len": 20,
        "ema_trend": 200,
        "adx_len": 14,
        "adx_min": 20.0,
        "atr_len": 20,
        "sl_atr": 2.5,
        "trail_atr": 3.0,
        "rr": 8.0,  # a far "moonshot" target; the trailing stop does most exits
        "max_hold_bars": 200,
    }

    @property
    def warmup(self) -> int:
        return int(self.params["ema_trend"]) + 60

    def _populate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        df["dc_upper"], df["dc_lower"] = ta.donchian(df, p["entry_len"])
        df["dc_exit_upper"], df["dc_exit_lower"] = ta.donchian(df, p["exit_len"])
        df["ema_trend"] = ta.ema(c, p["ema_trend"])
        df["adx"], _, _ = ta.adx(df, p["adx_len"])
        df["atr"] = ta.atr(df, p["atr_len"])
        trending = df["adx"] > p["adx_min"]
        df["enter_long"] = (c > df["dc_upper"]) & (c > df["ema_trend"]) & trending
        df["enter_short"] = (c < df["dc_lower"]) & (c < df["ema_trend"]) & trending
        df["long_sl"], df["long_tp"], df["short_sl"], df["short_tp"] = rr_targets(
            c, df["atr"], p["sl_atr"], p["rr"]
        )
        df["trail_dist"] = p["trail_atr"] * df["atr"]
        df["exit_long"] = c < df["dc_exit_lower"]
        df["exit_short"] = c > df["dc_exit_upper"]
        return df

    def explain(self, row: pd.Series, side: str) -> str:
        word = "high" if side == "long" else "low"
        return (
            f"new {self.params['entry_len']}-bar {word} in a trending market (ADX {row['adx']:.0f}); "
            f"no fixed target - the stop trails {self.params['trail_atr']:g} ATR behind once +1R"
        )
