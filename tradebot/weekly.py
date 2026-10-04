"""The weekly check-in (`tradebot weekly`): reconcile the last week, rerun the reference backtest
on frozen data, summarise uptime, and append a one-page entry to STATUS.md.

The entry has four parts: what changed (code, settings, strategy selection since last week),
what the numbers say (with the lines that reproduce them), what needs the owner (flags, never
decisions taken here) and what's next (the top open items of BACKLOG.md). Reporting only.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pandas as pd


REPO = Path(__file__).resolve().parent.parent
UPTIME_FLAG = 0.95  # less uptime than this needs the owner's attention


# --------------------------------------------------------------------- state between weeks
def state_path(state_dir) -> Path:
    return Path(state_dir) / "research" / "weekly.json"


def load_state(state_dir) -> dict:
    p = state_path(state_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def save_state(state_dir, data: dict) -> None:
    p = state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, default=str))


def git_log(since_commit: str | None, until: str = "HEAD", limit: int = 12) -> list[str]:
    if not since_commit:
        return []
    try:
        out = subprocess.run(["git", "log", "--oneline", f"-{limit}", f"{since_commit.split('+')[0]}..{until}"],
                             cwd=REPO, capture_output=True, text=True, timeout=10)
        return [x for x in out.stdout.splitlines() if x.strip()] if out.returncode == 0 else []
    except Exception:
        return []


def backlog_next(path: Path, n: int = 3) -> list[str]:
    """The first ``n`` open items of BACKLOG.md ('- [ ] **title** ...' lines)."""
    if not path.exists():
        return []
    items = []
    for line in path.read_text().splitlines():
        m = re.match(r"\s*- \[ \] (.+)", line)
        if m:
            items.append(re.sub(r"\*\*(.+?)\*\*", r"\1", m.group(1)).split(" - ")[0].strip())
        if len(items) >= n:
            break
    return items


# --------------------------------------------------------------------- summaries
def _dur_h(ms: float) -> str:
    return f"{ms / 3_600_000:.1f}h"


def uptime_summary(rec) -> tuple[list[str], dict]:
    up = rec.uptime
    kinds = {"computer asleep": 0, "running but not looping": 0, "stopped": 0, "without a clean stop": 0,
             "no record": 0, "before the start/stop/sleep log": 0}
    down = {k: 0 for k in kinds}
    for g in up.gaps:
        for k in kinds:
            if k in g.cause:
                kinds[k] += 1
                down[k] += g.end - g.start
                break
    worst = sorted(((rec.uptime.down_ms(d, d + 86_400_000), d) for d in
                    range(rec.since_ms // 86_400_000 * 86_400_000, rec.end_ms, 86_400_000)), reverse=True)[:3]
    lines = [f"- Uptime {rec.uptime_share:.0%} of the week; {len(up.gaps)} gap{'' if len(up.gaps) == 1 else 's'}"
             + (": " + ", ".join(f"{n} {k} ({_dur_h(down[k])})" for k, n in kinds.items() if n) if up.gaps else "")]
    if worst and worst[0][0] > 0:
        lines.append("  Most down: " + ", ".join(f"{pd.Timestamp(d, unit='ms', tz='UTC'):%a %m-%d} {_dur_h(ms)}"
                                                 for ms, d in worst if ms > 0))
    return lines, {"uptime": rec.uptime_share, "gaps": len(up.gaps), "crashes": kinds["without a clean stop"],
                   "unexplained_gaps": kinds["no record"]}


def reconcile_summary(rec) -> tuple[list[str], dict]:
    rows = rec.rows
    both = [r for r in rows if not r.paper.startswith("(no signal") and not r.backtest.startswith("(")]
    bt_only = [r for r in rows if r.paper.startswith("(no signal")]
    paper_only = [r for r in rows if r.backtest.startswith("(")]
    first = [r for r in both if r.cause.startswith("first")]
    knock = [r for r in both if r.cause.startswith("knock")]
    reasons = pd.Series([r.paper[len("(no signal: "):-1].split(" (")[0] for r in bt_only]).value_counts()
    fills = [r for r in rows if r.entry_bps is not None]
    closed = [r for r in fills if r.exit_paper and r.exit_paper != "still open"]
    late = [r for r in closed if r.exit_late_h is not None and r.exit_late_h > 0.25]
    lines = [f"- Signals: backtest {len(rows) - len(paper_only)}, paper {len(rows) - len(bt_only)}, both {len(both)}; "
             f"taken vs skipped agree {len(both) - len(first) - len(knock)} of {len(both)} "
             f"({len(first)} first differences, {len(knock)} knock-on)"]
    if len(reasons):
        lines.append("  Backtest only: " + ", ".join(f"{n} {k}" for k, n in reasons.items()))
    for r in first[:3]:
        lines.append(f"  First difference {r.time} {r.symbol} {r.setup}: {r.cause[len('first difference: '):]}")
    if fills:
        mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")  # noqa: E731
        lines.append(f"- Fills: {len(fills)} shared entries, entry {mean([r.entry_bps for r in fills]):+.1f} bps vs "
                     f"modelled; {len(closed)} shared exits"
                     + (f", exit {mean([r.exit_bps for r in closed]):+.0f} bps, R paper - backtest "
                        f"{mean([r.r_paper - r.r_model for r in closed]):+.2f}" if closed else "")
                     + (f"; {len(late)} exited late" if late else ""))
        for r in late[:3]:
            lines.append(f"  Late exit {r.time} {r.symbol}: {r.exit_paper} vs {r.exit_model}, {r.exit_bps:+.0f} bps, "
                         f"{r.exit_late_h:.1f}h late - {r.exit_why_late}")
    days = [d for d in rec.core_days if d["paper"] is not None or d["backtest"] is not None]
    same = sum(d["paper"] is not None and d["backtest"] is not None and abs(d["paper"] - d["backtest"]) < 1e-9
               for d in days)
    if days or rec.core_trades:
        lines.append(f"- Core: targets match {same} of {len(days)} coin-days; rebalance rule differs on "
                     f"{len(rec.core_rule_diffs)} coin-days; {len(rec.core_trades)} trades"
                     + (f"; {len(rec.core_days_missing)} days with no core check" if rec.core_days_missing else ""))
    unexplained = int(sum(n for k, n in reasons.items() if k.startswith(("no scan recorded", "scanned"))))
    return lines, {"first_differences": len(first), "core_target_mismatch": len(days) - same,
                   "core_rule_diffs": len(rec.core_rule_diffs), "late_exits": len(late),
                   "signal_data_differences": unexplained, "shared": len(both)}


def portfolio_summary(res) -> tuple[list[str], dict]:
    from .portfolio import account_stats

    start = None
    if res.satellite_from is not None:
        t = pd.Timestamp(res.satellite_from)
        start = (t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")).floor("D")
    st = account_stats(res, start)
    names = (("combined", f"{res.fraction:.0%}/{1 - res.fraction:.0%}"), ("core", "core only"),
             ("matched", f"core at same exposure ({st['matched'].get('k', 0):.0%})"), ("satellite", "satellite only"))
    lines = [f"- Frozen reference rerun, from the satellite's first trade "
             f"({start:%Y-%m-%d}):" if start is not None else "- Frozen reference rerun (no satellite trades):"]
    for key, label in names:
        x = st[key]
        lines.append(f"  {label:<32} {x['cagr_pct']:+6.1f}%/yr, worst dip {-abs(x['max_dd_pct']):6.1f}%, "
                     f"Sharpe {x['sharpe']:.2f}")
    return lines, {k: {"sharpe": round(st[k]["sharpe"], 3), "cagr": round(st[k]["cagr_pct"], 2),
                       "dip": round(abs(st[k]["max_dd_pct"]), 2)} for k, _ in names}


# --------------------------------------------------------------------- the entry
def needs_owner(now: dict, prev: dict, settings_diffs: list[str], selection_changed: bool) -> list[str]:
    out = []
    r = now.get("reconcile") or {}
    if now.get("uptime", {}).get("uptime", 1.0) < UPTIME_FLAG:
        out.append(f"Uptime {now['uptime']['uptime']:.0%} (under {UPTIME_FLAG:.0%}): paper can't be compared "
                   f"with the backtest while the bot is down")
    if now.get("uptime", {}).get("crashes"):
        out.append(f"{now['uptime']['crashes']} run(s) ended without a clean stop (crash, kill or power loss)")
    if r.get("first_differences"):
        out.append(f"{r['first_differences']} first difference(s) between paper and the backtest's decisions")
    if r.get("signal_data_differences"):
        out.append(f"{r['signal_data_differences']} backtest signal(s) live didn't see although it scanned "
                   f"(live candles differ, or a scan was not recorded)")
    if r.get("core_target_mismatch") or r.get("core_rule_diffs"):
        out.append("The core differs from its backtest (targets or rebalances)")
    if settings_diffs:
        out.append("Settings changed since last week: " + "; ".join(settings_diffs[:5]))
    if selection_changed:
        out.append("Learn changed the strategy selection (in-sample: informational, not evidence)")
    old, new = (prev.get("numbers") or {}).get("portfolio"), now.get("portfolio")
    if old and new and prev.get("reference") == now.get("reference"):
        moved = [k for k in new if k in old and abs(new[k]["sharpe"] - old[k]["sharpe"]) >= 0.01]
        if moved:
            out.append("The frozen reference rerun moved since last week ("
                       + ", ".join(f"{k} Sharpe {old[k]['sharpe']:.2f} -> {new[k]['sharpe']:.2f}" for k in moved)
                       + "): check whether a conclusion changes")
    return out


def status_entry(*, date: str, code: str, settings: str, changed: list[str], numbers: list[str],
                 reproduce: list[str], needs: list[str], next_items: list[str]) -> str:
    lines = [f"## {date} - weekly check-in (`tradebot weekly`)", "",
             f"Code {code} · settings {settings}", "",
             "**What changed**"] + [f"- {x}" for x in (changed or ["nothing since last week's check-in"])]
    lines += ["", "**What the numbers say**"] + numbers
    lines += ["", "Reproduce:"] + [f"    {x}" for x in reproduce]
    lines += ["", "**What needs you**"] + [f"- {x}" for x in (needs or ["nothing"])]
    lines += ["", "**What's next** (BACKLOG.md)"] + [f"- {x}" for x in (next_items or ["the backlog is empty"])]
    return "\n".join(lines) + "\n"


def append_status(path: Path, entry: str) -> None:
    path = Path(path)
    head = "" if path.exists() else ("# Status\n\nOne entry per session or weekly check-in, newest last. Each is "
                                     "under a page: what changed, what the numbers say (with reproduce lines), "
                                     "what needs the owner, what's next.\n")
    with open(path, "a") as fh:
        fh.write(head + "\n" + entry)


def today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())
