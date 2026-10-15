"""A point-in-time universe: the coins the universe rule would have picked on each day, delisted
coins included (BACKLOG: the backtests use today's most traded coins, which flatters the satellite).

The source is Binance's public archive (data.binance.vision): monthly files of daily candles for
every spot pair it ever listed, with each day's quote volume. The rule is the live one
(universe.select_universe, exchange.top_symbols), applied on each day D:
- the pairs in the quote currency that traded on D-1, ranked by D-1's quote volume (what the bot
  could know on D; live ranks a rolling 24 hours every few hours), at least ``min_quote_volume``;
- stablecoins, pegged (a daily range under 0.3%) and leveraged tokens excluded, as live, plus stable
  and fiat tickers live's list lacks because they no longer trade (HISTORICAL_PEGGED);
- of the first 2 x top_n, those that traded around D - min_history_days (a candle in the 3 days up
  to it, as listed_before asks the exchange), then the first top_n.

Reporting only: any use of it in a verdict goes through a registered protocol (CLAUDE.md). A pair
the archive files two coins under (a long gap and a price jump: Terra's LUNA and the new LUNA) is
flagged in ``reused``: never splice its candles across the gap.
"""
from __future__ import annotations

import io
import json
import math
import re
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

from .data.exchange import LEVERAGED, PEGGED_RANGE, STABLECOINS

ARCHIVE = "https://data.binance.vision/"
LISTING = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
COLUMNS = ["open", "high", "low", "close", "volume", "quote_volume"]
DAY = pd.Timedelta(days=1)
HISTORICAL_PEGGED = {"PAX", "USDSB", "BKRW", "AUD", "KGST"}  # stable and fiat tickers that no longer trade
RENAMES = {"MATIC": "POL", "FTM": "S", "EOS": "A", "RNDR": "RENDER", "BTT": "BTTC", "MKR": "SKY", "TOMO": "VIC",
           "OCEAN": "FET", "AGIX": "FET", "ERD": "EGLD", "NPXS": "PUNDIX", "KLAY": "KAIA", "NANO": "XNO",
           "BCC": "BCHABC", "BCHABC": "BCH", "GAL": "G"}  # the coin trades on under the new ticker
GAP_DAYS = 7  # a longer stretch with no daily candle splits a pair into separate listings
LISTED_WINDOW = pd.Timedelta(days=3)  # listed_before asks for candles from 3 days before the cutoff


def _get(url: str, timeout: float = 60, tries: int = 4) -> bytes:
    for k in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https hosts
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or k == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if k == tries - 1:
                raise
        time.sleep(2 ** k)
    raise RuntimeError("unreachable")


def _list(prefix: str, delimiter: str = "") -> list[str]:
    """Keys (or, with a delimiter, sub-prefixes) under a prefix of the archive's bucket, every page."""
    out, marker = [], ""
    tag = "Prefix" if delimiter else "Key"
    while True:
        url = f"{LISTING}?prefix={quote(prefix, safe='/')}" + (f"&delimiter={quote(delimiter)}" if delimiter else "") \
            + (f"&marker={quote(marker, safe='/')}" if marker else "")
        page = _get(url).decode()
        found = [x for x in re.findall(rf"<{tag}>([^<]+)</{tag}>", page) if x != prefix]
        out += found
        if "<IsTruncated>true</IsTruncated>" not in page:
            return out
        nxt = re.search(r"<NextMarker>(.*?)</NextMarker>", page)  # only sent with a delimiter
        marker = nxt.group(1) if nxt else (found[-1] if found else "")
        if not marker:
            return out


def archive_pairs(quote_ccy: str) -> list[str]:
    """Every spot pair in the quote currency the archive has daily candles for, as 'BASE/QUOTE'."""
    names = [p.rstrip("/").rsplit("/", 1)[-1] for p in _list("data/spot/monthly/klines/", "/")]
    return sorted(f"{n[:-len(quote_ccy)]}/{quote_ccy}" for n in names
                  if n.endswith(quote_ccy) and len(n) > len(quote_ccy))


def _months(name: str) -> list[str]:
    keys = [k for k in _list(f"data/spot/monthly/klines/{name}/1d/") if k.endswith(".zip")]
    return sorted(k.rsplit("-1d-", 1)[1][:7] for k in keys)


def latest_month(quote_ccy: str) -> str:
    """The newest month the archive has for BTC: monthly files appear days after a month ends."""
    return _months(f"BTC{quote_ccy}")[-1]


def parse_month(raw: bytes) -> pd.DataFrame:
    """One monthly zip of daily candles -> open, high, low, close, volume, quote_volume by day."""
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode()
    rows = [line.split(",") for line in text.splitlines() if line and line[0].isdigit()]
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    ts = pd.Series([int(r[0]) for r in rows])
    ts = ts.where(ts < 10**14, ts // 1000)  # the archive moved to microseconds in 2025
    df = pd.DataFrame([[float(r[i]) for i in (1, 2, 3, 4, 5, 7)] for r in rows], columns=COLUMNS)
    df.index = pd.to_datetime(ts, unit="ms", utc=True)
    df.index.name = "time"
    return df


def _month_end(month: str) -> pd.Timestamp:
    return pd.Period(month, "M").end_time.tz_localize("UTC").normalize()


def fetch_pair(store, pair: str, through: str) -> pd.DataFrame:
    """The pair's daily candles from the archive's monthly files up to ``through`` ('YYYY-MM'),
    kept in ``store`` (an OHLCVStore): later calls fetch only months not yet kept, and the last
    kept month again if it stopped before its end."""
    have = store.load(pair, "1d")
    name = pair.replace("/", "")
    done = set(have.index.strftime("%Y-%m")) if len(have) else set()
    if len(have):
        last = have.index[-1].strftime("%Y-%m")
        if have.index[-1] < _month_end(last):
            done.discard(last)
    todo = [m for m in _months(name) if m <= through and m not in done]
    if todo:
        parts = [have] + [parse_month(_get(ARCHIVE + quote(f"data/spot/monthly/klines/{name}/1d/{name}-1d-{m}.zip")))
                          for m in todo]
        have = pd.concat([p for p in parts if len(p)])
        have = have[~have.index.duplicated(keep="last")].sort_index()
        store.save(pair, "1d", have)
    return have[have.index <= _month_end(through)] if len(have) else have


def fetch_all(store, pairs: list[str], through: str, workers: int = 8, log_fn=print) -> dict[str, pd.DataFrame]:
    out, failed = {}, []

    def one(pair):
        try:
            return pair, fetch_pair(store, pair, through)
        except Exception as exc:  # noqa: BLE001 - reported together below
            failed.append(f"{pair}: {exc}")
            return pair, None

    with ThreadPoolExecutor(workers) as pool:
        for n, (pair, df) in enumerate(pool.map(one, pairs), 1):
            if df is not None and len(df):
                out[pair] = df
            if n % 100 == 0:
                log_fn(f"  {n}/{len(pairs)} pairs")
    if failed:
        raise RuntimeError(f"{len(failed)} pairs could not be fetched (rerun: kept months are not fetched again): "
                           + "; ".join(failed[:5]))
    return out


def excluded(pair: str) -> bool:
    base = pair.split("/")[0]
    return base in STABLECOINS or base in HISTORICAL_PEGGED or bool(LEVERAGED.search(base))


def segments(df: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """The pair's listings: runs of daily candles with no gap longer than GAP_DAYS."""
    idx = df.index
    breaks = np.flatnonzero(np.diff(idx.values).astype("timedelta64[D]").astype(int) > GAP_DAYS)
    starts, ends = [0, *(breaks + 1)], [*breaks, len(idx) - 1]
    return [(idx[a], idx[b]) for a, b in zip(starts, ends)]


def reused(df: pd.DataFrame) -> list[str]:
    """Gaps after which the price is more than 10x away: another coin under the same ticker."""
    out, segs = [], segments(df)
    for (_, end), (start, _) in zip(segs, segs[1:]):
        a, b = float(df.loc[end, "close"]), float(df.loc[start, "close"])
        if a > 0 and b > 0 and abs(math.log10(b / a)) > 1:
            out.append(f"{end:%Y-%m-%d}..{start:%Y-%m-%d}")
    return out


def build(daily: dict[str, pd.DataFrame], *, top_n: int, min_quote_volume: float, min_history_days: float,
          start: str, end: str) -> dict:
    """The universe on each day from start to end (inclusive, UTC dates)."""
    vol, pegged, opens = {}, {}, {}
    for p, df in daily.items():
        if not len(df) or excluded(p):
            continue
        vol[p] = df["quote_volume"]
        pegged[p] = (df["high"] - df["low"]) / df["close"].where(df["close"] > 0) < PEGGED_RANGE
        opens[p] = df.index.values
    volumes = pd.DataFrame(vol).sort_index()
    pegs = pd.DataFrame(pegged).reindex(volumes.index).fillna(False).astype(bool)

    def traded_around(p: str, cutoff: pd.Timestamp) -> bool:  # universe.listed_before
        i = np.searchsorted(opens[p], (cutoff - LISTED_WINDOW).to_datetime64())
        return i < len(opens[p]) and opens[p][i] <= cutoff.to_datetime64()

    days = {}
    for d in pd.date_range(start, end, freq="D", tz="UTC"):
        prev = d - DAY
        row = volumes.loc[prev].dropna() if prev in volumes.index else pd.Series(dtype=float)
        row = row[(row >= min_quote_volume) & ~pegs.loc[prev, row.index]] if len(row) else row
        ranked = list(row.sort_values(ascending=False, kind="stable").index)
        if min_history_days > 0:
            cutoff = d - pd.Timedelta(days=min_history_days)
            ranked = [p for p in ranked[:2 * top_n] if traded_around(p, cutoff)]
        days[d.strftime("%Y-%m-%d")] = ranked[:top_n]
    listed = {p: [[f"{a:%Y-%m-%d}", f"{b:%Y-%m-%d}"] for a, b in segments(df)] for p, df in sorted(daily.items())
              if len(df)}
    return {"end": end, "days": days, "listed": listed,
            "reused": {p: r for p, df in sorted(daily.items()) if len(df) and (r := reused(df))}}


def survivorship(universe: dict, today: list[str] | None = None) -> list[dict]:
    """Per year: the coins in the list at some point, how many stopped trading before the end (a
    listing that ended; a renamed coin counts as renamed, not stopped), their share of member-days,
    and how many are not in ``today``'s list."""
    end = universe["end"]
    rows = {}
    for day, members in universe["days"].items():
        prev = (pd.Timestamp(day) - DAY).strftime("%Y-%m-%d")
        y = rows.setdefault(day[:4], {"year": day[:4], "coins": set(), "gone": set(), "renamed": set(),
                                      "member_days": 0, "gone_days": 0})
        for p in members:
            seg = next((s for s in universe["listed"][p] if s[0] <= prev <= s[1]), universe["listed"][p][-1])
            last = seg is universe["listed"][p][-1]
            y["coins"].add(p)
            y["member_days"] += 1
            if seg[1] < end and last and p.split("/")[0] in RENAMES:
                y["renamed"].add(p)
            elif seg[1] < end:
                y["gone"].add(p)
                y["gone_days"] += 1
    out = []
    for y in rows.values():
        out.append({"year": y["year"], "coins": len(y["coins"]), "gone": len(y["gone"]), "renamed": len(y["renamed"]),
                    "member_days": y["member_days"], "gone_days": y["gone_days"],
                    "gone_share": y["gone_days"] / y["member_days"] if y["member_days"] else 0.0,
                    "not_today": len(y["coins"] - set(today)) if today is not None else None})
    return out


def format_survivorship(rows: list[dict], today_label: str | None) -> str:
    lines = ["  year  coins in the list  stopped trading since  renamed  their share of member-days"
             + (f"  not in {today_label}" if today_label else "")]
    for r in rows:
        lines.append(f"  {r['year']}  {r['coins']:>17}  {r['gone']:>21}  {r['renamed']:>7}  {r['gone_share']:>26.0%}"
                     + (f"  {r['not_today']:>{len(today_label) + 7}}" if today_label else ""))
    return "\n".join(lines)


def save(path, universe: dict, meta: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps({**meta, **universe}, separators=(",", ":")))
