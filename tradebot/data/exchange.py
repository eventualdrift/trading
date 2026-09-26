"""Exchange access through ccxt (supports 100+ exchanges with one API)."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

import ccxt
import pandas as pd

from ..timeframes import tf_ms

log = logging.getLogger(__name__)

STABLECOINS = {
    "USDT", "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "USDD", "PYUSD", "USDE", "USD1",
    "EUR", "EURI", "AEUR", "GBP", "TRY", "BRL", "ZAR", "UST", "USTC", "PAXG", "XAUT", "WBTC", "WBETH",
}
LEVERAGED = re.compile(r"(UP|DOWN|BULL|BEAR|\d+[LS])$")


def ohlcv_to_df(rows: list[list[Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts").astype("int64"), unit="ms", utc=True)
    df.index.name = "time"
    df = df.astype(float)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def with_retries(fn: Callable, *args, attempts: int = 5, base_delay: float = 1.0, **kwargs):
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except ccxt.NetworkError as exc:  # timeouts, rate limits, maintenance, connection resets
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1)
            log.warning("%s failed (%s); retrying in %.0fs", getattr(fn, "__name__", fn), exc, delay)
            time.sleep(delay)


class ExchangeClient:
    """Market data + account access for one exchange."""

    price_bar_ms = 60_000  # 1-minute candles are used to watch open positions

    def __init__(
        self,
        exchange_id: str,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
        sandbox: bool = False,
        market_type: str = "spot",
    ):
        if not hasattr(ccxt, exchange_id):
            raise ValueError(f"Unknown exchange {exchange_id!r} (see ccxt.exchanges)")
        params: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": market_type}}
        if api_key:
            params.update(apiKey=api_key, secret=secret)
        if password:
            params["password"] = password
        self.ex = getattr(ccxt, exchange_id)(params)
        if market_type == "spot":  # don't load futures/options markets (other hosts, slower start-up)
            fm = self.ex.options.get("fetchMarkets")
            if isinstance(fm, dict) and "types" in fm:
                self.ex.options["fetchMarkets"] = {**fm, "types": ["spot"]}
            elif isinstance(fm, list):
                self.ex.options["fetchMarkets"] = ["spot"]
        if sandbox:
            self.ex.set_sandbox_mode(True)
        self.id = exchange_id
        self._markets: dict | None = None

    # ------------------------------------------------------------------ data
    @property
    def markets(self) -> dict:
        if self._markets is None:
            self._markets = with_retries(self.ex.load_markets)
        return self._markets

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def fetch_ohlcv_df(self, symbol: str, tf: str, limit: int = 300, since: int | None = None) -> pd.DataFrame:
        rows = with_retries(self.ex.fetch_ohlcv, symbol, tf, since, limit)
        return ohlcv_to_df(rows)

    def history(self, symbol: str, tf: str, start_ms: int, end_ms: int | None = None) -> pd.DataFrame:
        end_ms = end_ms or self.now_ms()
        step = tf_ms(tf)
        since, chunks = start_ms, []
        while since < end_ms:
            rows = with_retries(self.ex.fetch_ohlcv, symbol, tf, since, 1000)
            if not rows:
                break
            chunks.extend(rows)
            last = rows[-1][0]
            if last + step >= end_ms or last < since:
                break
            since = last + step
        return ohlcv_to_df(chunks) if chunks else ohlcv_to_df([])

    def fetch_last_price_ts(self, symbol: str) -> tuple[float, int | None]:
        """(last price, exchange timestamp in ms or None) - lets the bot spot a stale feed."""
        t = with_retries(self.ex.fetch_ticker, symbol)
        price = t.get("last") or t.get("close")
        if not price and t.get("bid") and t.get("ask"):
            price = (t["bid"] + t["ask"]) / 2
        if not price:
            raise RuntimeError(f"No price available for {symbol}")
        ts = t.get("timestamp")
        return float(price), int(ts) if ts else None

    def fetch_price_bars(self, symbol: str, since_ms: int) -> pd.DataFrame:
        return self.fetch_ohlcv_df(symbol, "1m", limit=1000, since=since_ms)

    def fetch_last_price(self, symbol: str) -> float:
        t = with_retries(self.ex.fetch_ticker, symbol)
        price = t.get("last") or t.get("close")
        if not price and t.get("bid") and t.get("ask"):
            price = (t["bid"] + t["ask"]) / 2
        if not price:
            raise RuntimeError(f"No price available for {symbol}")
        return float(price)

    def top_symbols(
        self,
        quote: str,
        n: int,
        min_quote_volume: float = 0.0,
        whitelist: list[str] | None = None,
        blacklist: list[str] | None = None,
    ) -> list[str]:
        if whitelist:
            return [s for s in whitelist if s in self.markets and s not in (blacklist or [])]
        tickers = with_retries(self.ex.fetch_tickers)
        rows = []
        for sym, t in tickers.items():
            m = self.markets.get(sym)
            if not m or not m.get("active", True) or m.get("quote") != quote or not m.get("spot", True):
                continue
            base = m.get("base", "")
            if base in STABLECOINS or LEVERAGED.search(base) or sym in (blacklist or []):
                continue
            qv = t.get("quoteVolume") or (t.get("baseVolume") or 0) * (t.get("last") or 0)
            if qv and qv >= min_quote_volume:
                rows.append((qv, sym))
        rows.sort(reverse=True)
        return [s for _, s in rows[:n]]

    # --------------------------------------------------------------- trading
    def market(self, symbol: str) -> dict:
        return self.markets[symbol]

    def limits(self, symbol: str) -> dict:
        m = self.market(symbol)
        lim = m.get("limits") or {}
        return {
            "min_amount": (lim.get("amount") or {}).get("min") or 0.0,
            "min_cost": (lim.get("cost") or {}).get("min") or 0.0,
        }

    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return float(self.ex.amount_to_precision(symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        return float(self.ex.price_to_precision(symbol, price))
