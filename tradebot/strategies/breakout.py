from __future__ import annotations

import pandas as pd

from .. import indicators as ta
from .base import Strategy, rr_targets


class DonchianBreakout(Strategy):
    """Momentum breakout.

    Enter when price closes above the highest high of the previous N bars on
    above-average volume, in the direction of the long-term trend (EMA200).
    Exit early if price falls back below the shorter exit channel.
    """

    name = "breakout"
    description = "Channel breakout"
    default_params = {
        "entry_len": 20,
        "exit_len": 10,
        "ema_trend": 200,
        "volume_len": 20,
        "volume_mult": 1.5,
        "atr_len": 14,
        "sl_atr": 2.0,
        "rr": 2.5,
        "max_hold_bars": 80,
    }

    @property
    def warmup(self) -> int:
        return int(self.params["ema_trend"]) + 50

    def _populate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        df["dc_upper"], df["dc_lower"] = ta.donchian(df, p["entry_len"])
        df["dc_exit_upper"], df["dc_exit_lower"] = ta.donchian(df, p["exit_len"])
        df["ema_trend"] = ta.ema(c, p["ema_trend"])
        df["vol_avg"] = ta.sma(df["volume"], p["volume_len"]).shift(1)
        df["atr"] = ta.atr(df, p["atr_len"])
        vol_ok = df["volume"] > p["volume_mult"] * df["vol_avg"]

        df["enter_long"] = (c > df["dc_upper"]) & (c > df["ema_trend"]) & vol_ok
        df["enter_short"] = (c < df["dc_lower"]) & (c < df["ema_trend"]) & vol_ok
        df["long_sl"], df["long_tp"], df["short_sl"], df["short_tp"] = rr_targets(
            c, df["atr"], p["sl_atr"], p["rr"]
        )
        df["exit_long"] = c < df["dc_exit_lower"]
        df["exit_short"] = c > df["dc_exit_upper"]
        return df

    def explain(self, row: pd.Series, side: str) -> str:
        vol_x = row["volume"] / row["vol_avg"] if row["vol_avg"] else float("nan")
        level = row["dc_upper"] if side == "long" else row["dc_lower"]
        word = "above" if side == "long" else "below"
        return (
            f"closed {word} {self.params['entry_len']}-bar range ({level:.6g}) "
            f"on {vol_x:.1f}x average volume, with the EMA{self.params['ema_trend']} trend"
        )
