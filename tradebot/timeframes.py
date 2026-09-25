"""Timeframe helpers. All timestamps in this project are UTC milliseconds."""
from __future__ import annotations

import pandas as pd

TF_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
}


def tf_seconds(tf: str) -> int:
    try:
        return TF_SECONDS[tf]
    except KeyError:
        raise ValueError(f"Unsupported timeframe {tf!r}; use one of {list(TF_SECONDS)}") from None


def tf_ms(tf: str) -> int:
    return tf_seconds(tf) * 1000


def last_closed_open_ms(now_ms: int, tf: str) -> int:
    """Open time of the most recent *fully closed* candle at ``now_ms``."""
    step = tf_ms(tf)
    return (now_ms // step) * step - step


def drop_unclosed(df: pd.DataFrame, tf: str, now_ms: int) -> pd.DataFrame:
    """Remove the still-forming candle(s). Exchanges return the live candle last."""
    if df.empty:
        return df
    closed = (index_ms(df.index) + tf_ms(tf)) <= now_ms
    return df[closed]


def index_ms(index: pd.DatetimeIndex):
    """Datetime index -> int64 numpy array of epoch milliseconds (unit-agnostic)."""
    return index.as_unit("ms").asi8


def to_ms(ts: pd.Timestamp) -> int:
    return int(pd.Timestamp(ts).value // 1_000_000)


def from_ms(ms: int) -> pd.Timestamp:
    return pd.Timestamp(ms, unit="ms", tz="UTC")


def resample_ohlcv(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Aggregate a finer OHLCV frame into ``tf`` candles (labelled by open time)."""
    rule = f"{tf_seconds(tf)}s"
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["open", "close"])
