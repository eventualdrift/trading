"""Which coins the bot looks at - one rule for learning, backtests, the research commands and
the live bot, so they all trade the same universe.

The most liquid pairs by 24h volume (stablecoins, pegged and leveraged tokens excluded), and
with ``universe.min_history_days`` only coins that have traded for at least that long: new
listings are dropped and the next most liquid established coin takes their place.
"""
from __future__ import annotations

import logging

from .config import BotConfig

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
_LISTED: dict[tuple[str, str], int] = {}  # (market id, symbol) -> a date it is known to have traded on


def listed_before(market, symbol: str, cutoff_ms: int) -> bool:
    """Did ``symbol`` already trade on ``cutoff_ms``? (Its first daily candle is on or before it.)"""
    key = (getattr(market, "id", ""), symbol)
    known = _LISTED.get(key)
    if known is not None and known <= cutoff_ms:
        return True
    df = market.history(symbol, "1d", cutoff_ms - 3 * DAY_MS, cutoff_ms + 30 * DAY_MS)
    if df is None or df.empty:
        return False
    first = int(df.index[0].value // 1_000_000)
    if first <= cutoff_ms:
        _LISTED[key] = first
        return True
    return False


def select_universe(market, cfg: BotConfig, now_ms: int | None = None, log_fn=None) -> list[str]:
    u = cfg.universe
    q = cfg.exchange.quote
    if u.whitelist or u.min_history_days <= 0:
        return market.top_symbols(q, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
    now = now_ms or market.now_ms()
    cutoff = now - int(u.min_history_days * DAY_MS)
    ranked = market.top_symbols(q, u.top_n * 2, u.min_quote_volume, u.whitelist, u.blacklist)
    chosen, young = [], []
    for sym in ranked:
        if len(chosen) >= u.top_n:
            break
        try:
            old_enough = listed_before(market, sym, cutoff)
        except Exception as exc:  # unknown age: leave it out rather than trade a possible new listing
            log.warning("listing age of %s unknown (%s) - skipped", sym, exc)
            old_enough = False
        (chosen if old_enough else young).append(sym)
    if young and log_fn:
        log_fn(f"Universe: skipped {len(young)} coins listed less than {u.min_history_days:g} days ago: "
               + ", ".join(young))
    return chosen


def describe_universe(cfg: BotConfig, date: str) -> str:
    u = cfg.universe
    if u.whitelist:
        return "your universe.whitelist"
    age = f", traded for at least {u.min_history_days:g} days" if u.min_history_days > 0 else ""
    return f"today's top {u.top_n} {cfg.exchange.quote} pairs by 24h volume{age} ({date})"
