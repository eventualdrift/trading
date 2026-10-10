"""Gate 3, drawdown: limits set in advance from the backtest's own distribution.

``set_limits`` runs once, before the paper period it watches. It takes the frozen reference
backtest, like-for-like (from the satellite's first trade):
- the combined account's worst dip x 1.5 and the satellite sleeve's worst dip x 1.5;
- the longest run of losing satellite trades.
It keeps the backtest's daily returns and trade results for the range check, and records the limits
in the ledger. Changing them later is a registered test, never a reaction to a drawdown.

``check`` measures paper since the limits were set: the account's drawdown from its peak, the
satellite sleeve's (net of the core/satellite transfers), and the current losing run. When a
limit is hit, the caller reconciles the period and this module checks the range: how often the
backtest itself, resampled over a period that long, goes at least that deep (or loses that many
in a row). The outcome is one of:
- mechanics differ - fix them (the reconciliation shows where);
- in range - recorded, trading continues;
- outside range with correct mechanics - a registered test.
Trading continues throughout: it is paper.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

DIP_FACTOR = 1.5
LIMITS_ID = "drawdown-limits-v1"
P_IN_RANGE = 0.05  # in range when the backtest does at least this badly this often
BLOCK_DAYS = 10  # block bootstrap: keeps volatility clusters together
SIMS = 2000


def _folder(state_dir) -> Path:
    f = Path(state_dir) / "research"
    f.mkdir(parents=True, exist_ok=True)
    return f


def limits_path(state_dir) -> Path:
    return _folder(state_dir) / "drawdown-limits.json"


def load_limits(state_dir) -> dict | None:
    p = limits_path(state_dir)
    return json.loads(p.read_text()) if p.exists() else None


def max_drawdown(values) -> float:
    v = np.asarray(values, dtype=float)
    if len(v) < 2:
        return 0.0
    peak = np.maximum.accumulate(v)
    return float(np.max(1.0 - v / peak) * 100.0)


def longest_run(losses) -> int:
    best = cur = 0
    for lost in losses:
        cur = cur + 1 if lost else 0
        best = max(best, cur)
    return best


def trailing_run(losses) -> int:
    n = 0
    for lost in reversed(list(losses)):
        if not lost:
            break
        n += 1
    return n


# --------------------------------------------------------------------- the backtest side
def backtest_distribution(res) -> dict:
    """Like for like, from the satellite's first trade: dips, the longest losing run, and the
    daily returns and trade results the range check resamples."""
    from ..portfolio import worst_dip

    start = None
    if res.satellite_from is not None:
        t = pd.Timestamp(res.satellite_from)
        start = (t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")).floor("D")
    comb = res.curves[next(n for n in res.curves if n.startswith("Combined"))].dropna()
    sat = res.curves["Satellite only"].dropna()
    if start is not None:
        comb, sat = comb[comb.index >= start], sat[sat.index >= start]
    taken = sorted(res.satellite.taken if res.satellite else [], key=lambda t: t.exit_time)
    r = [float(t.r_multiple) for t in taken if t.reason != "end_of_data"]
    return {"window_start": str(start.date()) if start is not None else None,
            "combined_dip_pct": round(worst_dip(comb)[0], 2), "satellite_dip_pct": round(worst_dip(sat)[0], 2),
            "longest_losing_run": longest_run([x < 0 for x in r]), "trades": len(r),
            "combined_returns": [round(float(x), 6) for x in comb.pct_change().dropna()],
            "satellite_returns": [round(float(x), 6) for x in sat.pct_change().dropna()],
            "trade_r": [round(x, 4) for x in r]}


def set_limits(state_dir, dist: dict, *, reference: str, code: str, settings: str, paper_from_ms: int) -> dict:
    if load_limits(state_dir):
        raise ValueError("drawdown limits are already set: they are set once, in advance. Changing them is a "
                         "registered test, never a reaction to a drawdown.")
    limits = {"account_dd_pct": round(dist["combined_dip_pct"] * DIP_FACTOR, 2),
              "satellite_dd_pct": round(dist["satellite_dip_pct"] * DIP_FACTOR, 2),
              "losing_run": int(dist["longest_losing_run"])}
    data = {"id": LIMITS_ID, "set_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "paper_from_ms": int(paper_from_ms), "limits": limits,
            "rule": f"worst dips x {DIP_FACTOR} and the longest losing run, like for like from the satellite's "
                    f"first trade ({dist['window_start']})",
            "reference": reference, "code": code, "settings": settings, "backtest": dist}
    limits_path(state_dir).write_text(json.dumps(data, indent=1))
    from ..protocols import _append  # the ledger: these limits, before any paper period they watch

    _append(state_dir, {"id": LIMITS_ID, "kind": "limits", "at": data["set_at"], "limits": limits,
                        "paper_from_ms": data["paper_from_ms"], "reference": reference, "code": code,
                        "backtest": {k: dist[k] for k in ("combined_dip_pct", "satellite_dip_pct",
                                                          "longest_losing_run", "trades", "window_start")}})
    return data


# --------------------------------------------------------------------- the paper side
def paper_state(db, mode: str, since_ms: int) -> dict:
    snaps = db.snapshots(mode)
    if len(snaps):
        snaps = snaps[snaps.index >= pd.Timestamp(since_ms, unit="ms", tz="UTC")]
    out = {"snapshots": len(snaps), "account_dd_pct": 0.0, "account_peak_ms": None, "satellite_dd_pct": 0.0,
           "satellite_peak_ms": None, "days": 0.0}
    if len(snaps) >= 2:
        total = snaps["total"].astype(float)
        peak = total.cummax()
        out["account_dd_pct"] = round(float((1 - total.iloc[-1] / peak.iloc[-1]) * 100), 2)
        out["account_peak_ms"] = int(total.idxmax().value // 1_000_000)
        # the satellite net of transfers: amount > 0 moved satellite -> core, < 0 core -> satellite
        moves = pd.Series(0.0, index=snaps.index)
        for ts, d in db.botlog(mode, "core_transfer", since_ms):
            at = moves.index.searchsorted(pd.Timestamp(ts, unit="ms", tz="UTC"))
            if at < len(moves):
                moves.iloc[at] += float(d.get("amount") or 0.0)
        sat = snaps["satellite"].astype(float)
        rets = ((sat + moves) / sat.shift(1) - 1.0).fillna(0.0).replace([np.inf, -np.inf], 0.0)
        index = (1 + rets).cumprod()
        out["satellite_dd_pct"] = round(float((1 - index.iloc[-1] / index.cummax().iloc[-1]) * 100), 2)
        out["satellite_peak_ms"] = int(index.idxmax().value // 1_000_000)
        out["days"] = round((snaps.index[-1] - snaps.index[0]).total_seconds() / 86400, 2)
    closed = [p for p in db.closed_positions(mode, since_ms) if p.r_multiple is not None]
    losses = [p.r_multiple < 0 for p in sorted(closed, key=lambda p: p.closed_at)]
    out["trades"] = len(closed)
    out["losing_run"] = trailing_run(losses)
    run = closed[len(closed) - out["losing_run"]:] if out["losing_run"] else []
    out["run_start_ms"] = int(min(p.opened_at for p in run)) if run else None
    return out


def hits(limits: dict, state: dict) -> list[str]:
    lim = limits["limits"]
    out = []
    if state["account_dd_pct"] >= lim["account_dd_pct"]:
        out.append("account")
    if state["satellite_dd_pct"] >= lim["satellite_dd_pct"]:
        out.append("satellite")
    if lim["losing_run"] and state["losing_run"] >= lim["losing_run"]:
        out.append("losing_run")
    return out


# --------------------------------------------------------------------- the range check
def _bootstrap_dd(returns: list[float], days: int, rng) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    n = max(int(round(days)), 2)
    if len(r) < BLOCK_DAYS + 1:
        return np.zeros(SIMS)
    out = np.empty(SIMS)
    for k in range(SIMS):
        starts = rng.integers(0, len(r) - BLOCK_DAYS, size=n // BLOCK_DAYS + 1)
        path = np.concatenate([r[s:s + BLOCK_DAYS] for s in starts])[:n]
        out[k] = max_drawdown(np.concatenate([[1.0], np.cumprod(1 + path)]))
    return out


def range_check(limits: dict, state: dict, which: list[str], seed: int = 0) -> dict:
    """How often the backtest, resampled over a period as long as paper's, does at least as badly."""
    rng = np.random.default_rng(seed)
    bt = limits["backtest"]
    out = {}
    for w in which:
        if w in ("account", "satellite"):
            sims = _bootstrap_dd(bt["combined_returns" if w == "account" else "satellite_returns"],
                                 max(state["days"], 1), rng)
            p = float(np.mean(sims >= state[f"{w}_dd_pct"]))
        else:
            r = np.asarray(bt["trade_r"], dtype=float)
            n = max(state["trades"], state["losing_run"])
            runs = [longest_run(rng.choice(r, size=n, replace=True) < 0) for _ in range(SIMS)] if len(r) else [0]
            p = float(np.mean(np.asarray(runs) >= state["losing_run"]))
        out[w] = {"p": round(p, 4), "in_range": p >= P_IN_RANGE}
    return out


def outcome(ranges: dict, mechanics_ok: bool) -> str:
    if not mechanics_ok:
        return "mechanics differ"
    return "in range" if all(x["in_range"] for x in ranges.values()) else "outside range"


def _events_path(state_dir) -> Path:
    return _folder(state_dir) / "drawdown-events.jsonl"


def events(state_dir) -> list[dict]:
    p = _events_path(state_dir)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def record_event(state_dir, event: dict) -> None:
    with open(_events_path(state_dir), "a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")


def status_line(limits: dict | None, state: dict | None) -> str:
    if not limits:
        return "Drawdown gate: no limits set yet (tradebot gate drawdown --set-limits --reference <file>)"
    lim = limits["limits"]
    return (f"Drawdown gate: account {state['account_dd_pct']:.1f}% (limit {lim['account_dd_pct']:.1f}%), "
            f"satellite {state['satellite_dd_pct']:.1f}% (limit {lim['satellite_dd_pct']:.1f}%), "
            f"losing run {state['losing_run']} (limit {lim['losing_run']}), over {state['days']:.1f} days, "
            f"{state['trades']} trades")
