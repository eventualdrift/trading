"""Market vs limit entries, signal by signal.

Instance A enters with market orders (what the backtests assume); instance B follows A's
strategies (``learning.follow_state_dir``) and enters with a limit order at the signal
price. Both see the same signals, so each one can be compared directly:

* fill rate: how many of B's limit orders filled before they expired;
* price improvement: B's limit price vs A's market fill, in basis points and in R;
* missed signals: what the trade B missed did in A (its R), or why A has no result.

This is about execution only - not a P&L race between the two accounts, which also differ
by chance (different open positions, capacity, sizes).
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import pandas as pd

from .config import BotConfig
from .db import Database
from .models import Position, Signal

# Settings a limit-entry instance may change without confounding the comparison.
ALLOWED_DIFFERENCES = ("name", "state_dir", "costs.entry_order", "costs.maker_fee_rate", "telegram.",
                       "dashboard.", "learning.")


def _flatten(d: dict, prefix: str = "") -> dict[str, object]:
    out = {}
    for k, v in d.items():
        if isinstance(v, dict) and v:
            out.update(_flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def config_differences(a: BotConfig, b: BotConfig) -> list[str]:
    """Settings that differ between A and B beyond the entry order and instance plumbing."""
    fa, fb = (_flatten({k: v for k, v in asdict(c).items() if k != "secrets"}) for c in (a, b))
    out = []
    for key in sorted(set(fa) | set(fb)):
        if any(key == p or (p.endswith(".") and key.startswith(p)) for p in ALLOWED_DIFFERENCES):
            continue
        if fa.get(key) != fb.get(key):
            out.append(f"{key}: A={fa.get(key)!r} B={fb.get(key)!r}")
    follow = b.learning.follow_state_dir
    if not follow or Path(follow).resolve() != Path(a.state_dir).resolve():
        out.append(f"B does not follow A's strategies (learning.follow_state_dir should be {a.state_dir!r}) - "
                   f"the two may get different signals")
    if a.costs.entry_order != "market":
        out.append(f"A enters with {a.costs.entry_order!r} orders; the comparison expects A=market")
    if b.costs.entry_order != "limit":
        out.append(f"B enters with {b.costs.entry_order!r} orders; the comparison expects B=limit")
    return out


def _key(s: Signal) -> tuple:
    return (s.symbol, s.timeframe, s.strategy, s.side, int(s.candle_time))


def _side_state(sig: Signal | None, pos: Position | None) -> str:
    if sig is None:
        return "no signal"
    if sig.status != "opened" or pos is None:
        return f"{sig.status}" + (f": {sig.note}" if sig.note else "")
    return {"working": "limit working", "missed": "limit missed", "open": "filled", "closed": "filled",
            "failed": "failed", "unknown": "unknown", "pending": "pending"}.get(pos.status, pos.status)


@dataclass
class SignalRow:
    time: int
    symbol: str
    timeframe: str
    strategy: str
    side: str
    signal_price: float
    risk_per_unit: float
    a_state: str
    b_state: str
    a_fill: float | None = None
    b_fill: float | None = None
    improvement_bps: float | None = None  # + = the limit price was better than the market fill
    improvement_r: float | None = None
    a_r: float | None = None  # A's result on this signal (closed trades)
    a_exit: str | None = None

    @property
    def comparable(self) -> bool:
        """A filled at market and B placed its limit order: the like-for-like cases."""
        return self.a_state == "filled" and self.b_state in ("filled", "limit missed", "limit working")


def compare_entries(db_a: Database, db_b: Database, mode: str = "paper", since_ms: int | None = None) -> list[SignalRow]:
    b_sigs = db_b.signals_since(since_ms or 0)
    if since_ms is None:  # from B's first signal: A may have run alone before B started
        since_ms = min((s.created_at for s in b_sigs), default=0)
    a_sigs = db_a.signals_since(since_ms)
    a_by, b_by = {_key(s): s for s in a_sigs}, {_key(s): s for s in b_sigs}
    a_pos, b_pos = db_a.positions_by_signal(mode), db_b.positions_by_signal(mode)
    rows = []
    for key in sorted(set(a_by) | set(b_by), key=lambda k: (k[4], k[0])):
        sa, sb = a_by.get(key), b_by.get(key)
        pa = a_pos.get(sa.id) if sa is not None else None
        pb = b_pos.get(sb.id) if sb is not None else None
        ref = sa or sb
        row = SignalRow(time=ref.created_at, symbol=ref.symbol, timeframe=ref.timeframe, strategy=ref.strategy,
                        side=ref.side, signal_price=ref.entry, risk_per_unit=abs(ref.entry - ref.stop_loss),
                        a_state=_side_state(sa, pa), b_state=_side_state(sb, pb))
        if row.a_state == "filled":
            row.a_fill = pa.entry_price
            if pa.status == "closed":
                row.a_r, row.a_exit = pa.r_multiple, pa.exit_reason
        if row.b_state == "filled":
            row.b_fill = pb.entry_price
        if row.a_fill and row.b_fill:
            sign = 1.0 if row.side == "long" else -1.0
            row.improvement_bps = sign * (row.a_fill - row.b_fill) / row.a_fill * 1e4
            if row.risk_per_unit > 0:
                row.improvement_r = sign * (row.a_fill - row.b_fill) / row.risk_per_unit
        rows.append(row)
    return rows


def summarize(rows: list[SignalRow]) -> dict:
    comp = [r for r in rows if r.comparable]
    filled = [r for r in comp if r.b_state == "filled"]
    missed = [r for r in comp if r.b_state == "limit missed"]
    decided = len(filled) + len(missed)
    imp_bps = [r.improvement_bps for r in filled if r.improvement_bps is not None]
    imp_r = [r.improvement_r for r in filled if r.improvement_r is not None]
    missed_closed = [r for r in missed if r.a_r is not None]
    return {
        "signals": len(rows),
        "comparable": len(comp),
        "filled": len(filled),
        "missed": len(missed),
        "working": len(comp) - decided,
        "fill_rate": len(filled) / decided if decided else None,
        "improvement_bps_mean": sum(imp_bps) / len(imp_bps) if imp_bps else None,
        "improvement_bps_median": float(pd.Series(imp_bps).median()) if imp_bps else None,
        "improvement_r_sum": sum(imp_r),
        "missed_r_sum": sum(r.a_r for r in missed_closed),
        "missed_closed": len(missed_closed),
        "missed_winners": sum(1 for r in missed_closed if r.a_r > 0),
        "not_comparable": [r for r in rows if not r.comparable],
    }


def _t(ms: int) -> str:
    return pd.Timestamp(ms, unit="ms", tz="UTC").strftime("%m-%d %H:%M")


def _fmt_price(p: float | None) -> str:
    if p is None:
        return "-"
    return f"{p:,.2f}" if p >= 1000 else f"{p:.4f}" if p >= 1 else f"{p:.6g}"


def format_comparison(rows: list[SignalRow], a: BotConfig, b: BotConfig, diffs: list[str]) -> str:
    s = summarize(rows)
    fee_a, fee_b = a.costs.fee_rate, (b.costs.fee_rate if b.costs.maker_fee_rate is None else b.costs.maker_fee_rate)
    lines = [f"Entry comparison: A = market entries ({a.name or a.state_dir}), "
             f"B = limit at the signal price ({b.name or b.state_dir})"]
    if diffs:
        lines += ["", "WARNING - the two setups differ beyond the entry order, so differences may not be "
                      "caused by it:"] + [f"  - {d}" for d in diffs]
    lines += ["", f"Signals: {s['signals']} · comparable (A filled at market, B placed a limit): {s['comparable']}"]
    if s["fill_rate"] is not None:
        lines.append(f"Fill rate: {s['filled']} of {s['filled'] + s['missed']} limit orders filled "
                     f"({s['fill_rate']:.0%})" + (f"; {s['working']} still working" if s["working"] else ""))
    if s["improvement_bps_mean"] is not None:
        lines.append(f"Price improvement on fills (+ = limit better than A's market fill): "
                     f"mean {s['improvement_bps_mean']:+.1f} bps, median {s['improvement_bps_median']:+.1f} bps, "
                     f"total {s['improvement_r_sum']:+.2f}R over {s['filled']} fills")
        if a.mode == "paper":
            lines.append(f"  (A is paper: its fill is the last price + the modeled {a.costs.slippage_rate:.2%} slippage, "
                         f"so this mostly measures that assumption. Fill rate and missed outcomes use real prices.)")
    lines.append(f"Entry fees: A taker {fee_a:.3%}, B maker {fee_b:.3%} per side "
                 f"({(fee_a - fee_b) * 1e4:+.1f} bps in B's favour, not included above)")
    if s["missed"]:
        lines.append(f"Missed by B: {s['missed']} signal{'s' if s['missed'] != 1 else ''}; "
                     f"{s['missed_closed']} have a closed result in A: "
                     f"{s['missed_winners']} winners, total {s['missed_r_sum']:+.2f}R")
        if s["filled"]:
            lines.append(f"  -> better prices gained {s['improvement_r_sum']:+.2f}R on fills; missed trades were worth "
                         f"{s['missed_r_sum']:+.2f}R in A (net {s['improvement_r_sum'] - s['missed_r_sum']:+.2f}R "
                         f"for limit entries, before fees)")
    traded = {"filled", "limit missed", "limit working"}
    neither = [r for r in rows if r.a_state not in traded and r.b_state not in traded]
    shown = [r for r in rows if r not in neither]
    lines += ["", "Per signal (times UTC):",
              f"  {'time':<12}{'coin':<12}{'setup':<18}{'A (market)':<18}{'B (limit)':<18}"
              f"{'better':>9}{'in R':>7}  A's result"]
    for r in shown:
        a_txt = _fmt_price(r.a_fill) if r.a_state == "filled" else r.a_state.split(":")[0]
        b_txt = _fmt_price(r.b_fill) if r.b_state == "filled" else r.b_state.split(":")[0]
        imp = f"{r.improvement_bps:+.0f}bp" if r.improvement_bps is not None else ""
        imp_r = f"{r.improvement_r:+.2f}" if r.improvement_r is not None else ""
        res = (f"{r.a_r:+.2f}R {r.a_exit or ''}".strip() if r.a_r is not None else
               "open" if r.a_state == "filled" else "")
        if not r.comparable:  # only one side traded: say why the other didn't
            other = r.b_state if r.a_state in traded else r.a_state
            res = (res + " | " if res else "") + f"not comparable - {'B' if r.a_state in traded else 'A'} {other}"
        lines.append(f"  {_t(r.time):<12}{r.symbol:<12}{(r.side[0].upper() + ' ' + r.timeframe + ' ' + r.strategy)[:17]:<18}"
                     f"{a_txt[:17]:<18}{b_txt[:17]:<18}{imp:>9}{imp_r:>7}  {res}")
    if not rows:
        lines.append("  (no signals yet - both instances must run for a while)")
    if neither:
        lines.append(f"  + {len(neither)} signal{'s' if len(neither) != 1 else ''} neither instance traded "
                     f"(e.g. {neither[0].a_state})")
    return "\n".join(lines)


def write_csv(rows: list[SignalRow], path: str | Path) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[f.name for f in fields(SignalRow)])
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))
