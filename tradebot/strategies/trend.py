from __future__ import annotations

import pandas as pd

from .. import indicators as ta
from .base import Strategy, rr_targets


class TrendPullback(Strategy):
    """Buy dips inside an established trend.

    Trend: EMA50 above EMA200, price above EMA200 and ADX shows a real trend.
    Trigger: RSI dipped below the trigger level and just crossed back above it,
    i.e. the pullback is ending and the trend is resuming. Short side mirrored.
    Exit early if the trend regime flips (EMA50 crosses EMA200).
    """

    name = "trend"
    description = "Trend pullback"
    default_params = {
        "ema_fast": 20,
        "ema_mid": 50,
        "ema_slow": 200,
        "adx_len": 14,
        "adx_min": 20.0,
        "rsi_len": 14,
        "rsi_trigger": 45.0,
        "atr_len": 14,
        "sl_atr": 2.0,
        "rr": 2.0,
        "max_hold_bars": 60,
    }

    @property
    def warmup(self) -> int:
        return int(self.params["ema_slow"]) + 50

    def _populate(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        c = df["close"]
        df["ema_fast"] = ta.ema(c, p["ema_fast"])
        df["ema_mid"] = ta.ema(c, p["ema_mid"])
        df["ema_slow"] = ta.ema(c, p["ema_slow"])
        df["adx"], df["plus_di"], df["minus_di"] = ta.adx(df, p["adx_len"])
        df["rsi"] = ta.rsi(c, p["rsi_len"])
        df["atr"] = ta.atr(df, p["atr_len"])

        up = (df["ema_mid"] > df["ema_slow"]) & (c > df["ema_slow"]) & (df["adx"] > p["adx_min"])
        down = (df["ema_mid"] < df["ema_slow"]) & (c < df["ema_slow"]) & (df["adx"] > p["adx_min"])
        trig = p["rsi_trigger"]
        prev_rsi = df["rsi"].shift(1)
        df["enter_long"] = up & (prev_rsi < trig) & (df["rsi"] >= trig) & (c > df["ema_mid"])
        df["enter_short"] = (
            down & (prev_rsi > 100 - trig) & (df["rsi"] <= 100 - trig) & (c < df["ema_mid"])
        )
        df["long_sl"], df["long_tp"], df["short_sl"], df["short_tp"] = rr_targets(
            c, df["atr"], p["sl_atr"], p["rr"]
        )
        df["exit_long"] = df["ema_mid"] < df["ema_slow"]
        df["exit_short"] = df["ema_mid"] > df["ema_slow"]
        return df

    def explain(self, row: pd.Series, side: str) -> str:
        trend = "uptrend" if side == "long" else "downtrend"
        return (
            f"{trend} (EMA50 {'>' if side == 'long' else '<'} EMA200, ADX {row['adx']:.0f}); "
            f"pullback over, RSI turned {'up' if side == 'long' else 'down'} at {row['rsi']:.0f}"
        )
