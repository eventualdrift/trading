"""Local cache of historical candles (gzipped CSV per symbol/timeframe)."""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pandas as pd

from ..timeframes import drop_unclosed, index_ms, tf_ms

log = logging.getLogger(__name__)


class OHLCVStore:
    def __init__(self, root: str | Path, exchange_id: str):
        self.root = Path(root) / exchange_id

    def path(self, symbol: str, tf: str) -> Path:
        safe = symbol.replace("/", "_").replace(":", "_")
        return self.root / tf / f"{safe}.csv.gz"

    def load(self, symbol: str, tf: str) -> pd.DataFrame:
        p = self.path(symbol, tf)
        if not p.exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.read_csv(p, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "time"
        return df.astype(float)

    def save(self, symbol: str, tf: str, df: pd.DataFrame) -> None:
        """Write via a temp file + rename, so side-by-side instances sharing ``data.dir``
        never read a half-written file."""
        p = self.path(symbol, tf)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            df.to_csv(tmp, compression="gzip")
            os.replace(tmp, p)
        finally:
            tmp.unlink(missing_ok=True)

    def update(self, client, symbol: str, tf: str, days: int, now_ms: int | None = None) -> pd.DataFrame:
        """Bring the cached history up to date and return the last ``days`` of it."""
        now = now_ms or client.now_ms()
        start = now - int(days * 86_400_000)
        df = self.load(symbol, tf)
        if df.empty or index_ms(df.index)[0] > start + 2 * tf_ms(tf):
            fresh = client.history(symbol, tf, start, now)
        else:
            fresh = client.history(symbol, tf, int(index_ms(df.index)[-1]), now)
        merged = pd.concat([df, fresh]) if not df.empty else fresh
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        merged = drop_unclosed(merged, tf, now)
        if not merged.empty:
            self.save(symbol, tf, merged)
        return merged[merged.index >= pd.Timestamp(start, unit="ms", tz="UTC")]
