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


def selection_payload(selection) -> dict | None:
    """The whole selection (every combination with its strategy, timeframe, parameters and filters,
    and when learn made it), as saved in a universe file."""
    from dataclasses import asdict

    if selection is None:
        return None
    return {"created_at": selection.created_at, "combos": [asdict(c) for c in selection.combos]}


def saved_selection(frozen: dict, cfg: BotConfig):
    """The strategy selection a saved run used -> (Selection, where it came from), or None.

    Saved with the run when the file has it. Older files recorded only the strategy names: the
    selection is then recovered from the bot's activity log - the last one learn made before the
    run's data end with exactly those names."""
    import pandas as pd

    from .backtest.selection import ComboResult, Selection

    full = frozen.get("selection_full")
    if full and full.get("combos"):
        sel = Selection(created_at=full["created_at"], combos=[ComboResult(**c) for c in full["combos"]])
        return sel, "saved with the run"
    names = sorted(frozen.get("selection") or [])
    db_path = cfg.bot_db_path
    if not names or not db_path.exists():
        return None
    from .db import Database

    found = None
    for _, d in Database(db_path, readonly=bool(cfg.observe_state_dir)).botlog(cfg.mode, "selection"):
        made = d.get("created_at")
        combos = d.get("combos") or []
        if made is None or made * 1000 > frozen["data_end_ms"]:
            continue
        if sorted(f"{c['strategy']}@{c['timeframe']}" for c in combos) == names:
            found = (made, combos)
    if found is None:
        return None
    made, combos = found
    sel = Selection(created_at=made, combos=[ComboResult(c["strategy"], c["timeframe"], c.get("params") or {}, {}, {},
                                                         0, 0.0, True) for c in combos])
    when = pd.Timestamp(made, unit="s", tz="UTC").strftime("%Y-%m-%d %H:%M")
    return sel, f"recovered from the bot's activity log (learn of {when} UTC; the file saved only the strategy names)"


def candle_fingerprint(frame) -> dict:
    """What a run's candles for one coin and timeframe were, in a few numbers: the first and last
    candle, the row count and a hash of every candle (time, open, high, low, close, volume) - so a
    frozen rerun can tell a refilled cache from the data the saved run used."""
    import hashlib

    import numpy as np
    import pandas as pd

    idx = pd.DatetimeIndex(frame.index)
    idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
    h = hashlib.sha256()
    h.update(np.ascontiguousarray(idx.as_unit("ns").asi8 // 1_000_000).tobytes())
    cols = [c for c in ("open", "high", "low", "close", "volume") if c in getattr(frame, "columns", [])]
    for c in cols or [None]:
        values = (frame[c] if c else frame).to_numpy(dtype=float)
        h.update(np.ascontiguousarray(values).tobytes())
    return {"first": idx[0].strftime("%Y-%m-%d %H:%M") if len(idx) else None,
            "last": idx[-1].strftime("%Y-%m-%d %H:%M") if len(idx) else None,
            "rows": int(len(idx)), "sha256": h.hexdigest()[:16]}


def candle_pins(datasets: dict[str, dict], core_closes: dict | None = None) -> dict:
    """Fingerprints of every frame a run loaded: {symbol: {timeframe: fingerprint}}; the core's
    daily closes under their own key, as they span a longer history."""
    pins: dict[str, dict] = {}
    for tf, frames in (datasets or {}).items():
        for sym, df in frames.items():
            pins.setdefault(sym, {})[tf] = candle_fingerprint(df)
    for sym, s in (core_closes or {}).items():
        pins.setdefault(sym, {})["core-1d"] = candle_fingerprint(s)
    return pins


def compare_candles(saved: dict | None, now: dict) -> list[str] | None:
    """How this run's candles differ from the saved run's pins, one line per coin and timeframe
    (None: the saved run pinned nothing)."""
    if not saved:
        return None
    out = []
    for sym in sorted(set(saved) | set(now)):
        a, b = saved.get(sym) or {}, now.get(sym) or {}
        for tf in sorted(set(a) | set(b), key=lambda x: (x.startswith("core"), x)):
            x, y = a.get(tf), b.get(tf)
            name = f"{sym.split('/')[0]} {tf}"
            if x is None:
                out.append(f"{name}: not in the saved run, loaded now ({y['rows']} rows)")
            elif y is None:
                out.append(f"{name}: in the saved run ({x['rows']} rows), not loaded now")
            elif x.get("sha256") != y.get("sha256"):
                span = lambda f: f"{f['rows']} rows, {f['first']} to {f['last']}"  # noqa: E731
                out.append(f"{name}: {span(x)} then; {span(y)} now"
                           + ("" if (x["rows"], x["first"], x["last"]) != (y["rows"], y["first"], y["last"])
                              else " (same span, different values)"))
    return out


def save_universe(path, symbols: list[str], data_end_ms: int, source: str, selection_keys: list[str],
                  cfg: BotConfig, code: str | None = None, config: dict | None = None,
                  selection=None, run: dict | None = None, candles: dict | None = None) -> str:
    """Freeze a run's coin list, data end date and strategy selection so it can be reproduced
    (--universe-file); ``candles`` pins the candles it loaded (candle_pins)."""
    import json
    import time
    from pathlib import Path

    import pandas as pd

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    u = cfg.universe
    path.write_text(json.dumps({
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_end": pd.Timestamp(data_end_ms, unit="ms", tz="UTC").isoformat(),
        "data_end_ms": int(data_end_ms),
        "source": source,
        "symbols": list(symbols),
        "selection": list(selection_keys),
        "selection_full": selection_payload(selection),  # strategies, timeframes, parameters, filters
        "rules": {"top_n": u.top_n, "min_quote_volume": u.min_quote_volume,
                  "min_history_days": u.min_history_days, "whitelist": u.whitelist, "blacklist": u.blacklist},
        "code": code,  # git commit of the code that produced the run
        "config": config,  # the config file's settings, as loaded (provenance.settings_snapshot)
        **(run or {}),  # what the run itself used: timeframes_used, core_fraction_used
        "candles": candles,  # per coin and timeframe: first, last, rows, hash of the candles loaded
    }, indent=2, default=str))
    return str(path)


def load_universe(path) -> dict:
    import json
    from pathlib import Path

    data = json.loads(Path(path).read_text())
    if not data.get("symbols") or "data_end_ms" not in data:
        raise ValueError(f"{path} is not a saved universe (needs 'symbols' and 'data_end_ms')")
    return data
