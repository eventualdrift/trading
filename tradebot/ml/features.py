"""Feature engineering for the ML trade filter.

Market features are computed for every bar (causally). For a candidate trade they
are "aligned" to the trade direction - e.g. a +3% move is +3% for a long and -3%
for a short - so one model can learn from both sides.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .. import indicators as ta
from ..strategies import STRATEGIES
from ..timeframes import tf_seconds

RETURN_LAGS = (1, 3, 6, 12, 24, 48)
DIRECTIONAL = (
    [f"ret_{n}" for n in RETURN_LAGS]
    + ["rsi_14", "rsi_14_chg", "di_diff", "dist_ema20", "dist_ema50", "dist_ema200"]
    + ["ema50_slope", "bb_pctb", "range_pos", "body_frac"]
)
MARKET_COLUMNS = DIRECTIONAL + [
    "vol_12", "vol_48", "vol_ratio", "atr_pct", "adx", "bb_width", "vol_z",
    "upper_wick", "lower_wick", "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "btc_uptrend", "btc_vol_ratio",  # market regime from BTC (NaN when no context)
]
SIGNAL_COLUMNS = ["side", "tf_log_minutes", "stop_atr", "reward_risk"] + [
    f"strat_{name}" for name in sorted(STRATEGIES)
]
FEATURE_COLUMNS = MARKET_COLUMNS + SIGNAL_COLUMNS


def market_features(df: pd.DataFrame, context=None, tf: str | None = None) -> pd.DataFrame:
    o, h, l, c, v = (df[k] for k in ("open", "high", "low", "close", "volume"))
    f = pd.DataFrame(index=df.index)
    logc = np.log(c)
    lr = logc.diff()
    for n in RETURN_LAGS:
        f[f"ret_{n}"] = logc - logc.shift(n)
    f["vol_12"] = lr.rolling(12).std()
    f["vol_48"] = lr.rolling(48).std()
    f["vol_ratio"] = f["vol_12"] / f["vol_48"]
    atr_pct = ta.atr(df, 14) / c
    f["atr_pct"] = atr_pct
    r = ta.rsi(c, 14)
    f["rsi_14"] = r - 50.0
    f["rsi_14_chg"] = r.diff(3)
    adx, pdi, mdi = ta.adx(df, 14)
    f["adx"] = adx
    f["di_diff"] = pdi - mdi
    for n in (20, 50, 200):
        f[f"dist_ema{n}"] = (c / ta.ema(c, n) - 1.0) / atr_pct
    ema50 = ta.ema(c, 50)
    f["ema50_slope"] = ema50.pct_change(5) / atr_pct
    mid, up, lo = ta.bollinger(c, 20, 2.0)
    width = (up - lo).replace(0.0, np.nan)
    f["bb_pctb"] = (c - lo) / width - 0.5
    f["bb_width"] = width / mid
    f["vol_z"] = ta.zscore(np.log1p(v), 48)
    hi48, lo48 = h.rolling(48).max(), l.rolling(48).min()
    f["range_pos"] = (c - lo48) / (hi48 - lo48).replace(0.0, np.nan) - 0.5
    rng = (h - l).replace(0.0, np.nan)
    f["body_frac"] = (c - o) / rng
    f["upper_wick"] = (h - np.maximum(o, c)) / rng
    f["lower_wick"] = (np.minimum(o, c) - l) / rng
    hours = df.index.hour + df.index.minute / 60.0
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24.0)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24.0)
    f["dow_sin"] = np.sin(2 * np.pi * df.index.dayofweek / 7.0)
    f["dow_cos"] = np.cos(2 * np.pi * df.index.dayofweek / 7.0)
    if context is not None and len(df):
        from ..strategies.base import infer_tf

        ctx = context.align(df.index, tf or infer_tf(df.index))
        f["btc_uptrend"] = ctx["btc_uptrend"].to_numpy()
        f["btc_vol_ratio"] = ctx["btc_vol_ratio"].to_numpy()
    else:
        f["btc_uptrend"] = np.nan
        f["btc_vol_ratio"] = np.nan
    return f.replace([np.inf, -np.inf], np.nan)[MARKET_COLUMNS]


def candidate_features(
    mkt_rows: pd.DataFrame,
    sides: np.ndarray,
    strategies: np.ndarray,
    timeframe: str,
    close: np.ndarray,
    sl: np.ndarray,
    tp: np.ndarray,
) -> pd.DataFrame:
    """Build model inputs for candidate trades given their market-feature rows."""
    X = mkt_rows.copy()
    sign = np.where(np.asarray(sides) == "long", 1.0, -1.0)
    for col in DIRECTIONAL:
        X[col] = X[col].to_numpy() * sign
    up, down = X["upper_wick"].to_numpy().copy(), X["lower_wick"].to_numpy().copy()
    X["upper_wick"] = np.where(sign > 0, up, down)
    X["lower_wick"] = np.where(sign > 0, down, up)
    X["side"] = sign
    X["tf_log_minutes"] = np.log(tf_seconds(timeframe) / 60.0)
    risk = np.abs(np.asarray(close) - np.asarray(sl))
    atr_abs = X["atr_pct"].to_numpy() * np.asarray(close)
    with np.errstate(divide="ignore", invalid="ignore"):
        X["stop_atr"] = risk / atr_abs
        X["reward_risk"] = np.abs(np.asarray(tp) - np.asarray(close)) / risk
    strategies = np.asarray(strategies)
    for name in sorted(STRATEGIES):
        X[f"strat_{name}"] = (strategies == name).astype(float)
    return X[FEATURE_COLUMNS].replace([np.inf, -np.inf], np.nan)
