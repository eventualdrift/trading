"""How sensitive is the core's backtest to choices nobody validated? (reporting only)

1. Sleeve-reset timing: live resets the core/satellite split every 30 days counted from the day
   the core started, so the phase is an accident of the start date. The 65/35 account is
   replayed with each of the 30 possible phases.
2. Trend lengths: the 50/100/150/200-day set scaled by 0.8 and 1.2.

Only the spread is reported. Nothing here is selected or changes what the bot trades.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from .backtest.engine import Costs
from .config import BotConfig
from .core import simulate_core_detail, trend_weights_series
from .portfolio import (PortfolioBacktest, _matched_core, _ns, combine_sleeves_detail, curve_stats,
                        worst_dip)

SCALES = (0.8, 1.0, 1.2)


def _stats(curve: pd.Series, start: pd.Timestamp | None) -> dict | None:
    c = curve.dropna()
    if start is not None:
        c = c[c.index >= start]
    if len(c) < 30:
        return None
    s = curve_stats(c)
    dip, _, low = worst_dip(c)
    return {"cagr": s["cagr_pct"], "dip": dip, "low": low, "sharpe": s["sharpe"]}


def _first_day(res: PortfolioBacktest) -> pd.Timestamp | None:
    if res.satellite_from is None:
        return None
    t = pd.Timestamp(res.satellite_from)
    return (t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")).floor("D")


def windows(res: PortfolioBacktest, start: pd.Timestamp | None = None) -> list[tuple[str, pd.Timestamp | None]]:
    out = [("Full history" if start is None else f"From {start:%Y-%m-%d}", start)]
    if res.since is not None:
        out.append((f"Since {res.since:%Y-%m-%d}", max(res.since, start) if start is not None else res.since))
    first = _first_day(res)
    if first is not None:
        out.append((f"From the satellite's first trade ({first:%Y-%m-%d})", max(first, start) if start is not None else first))
    return out


def reset_phase_study(res: PortfolioBacktest) -> dict:
    """The 65/35 account (and the core at the same exposure) with every reset phase."""
    days = int(res.reset_days)
    if days <= 0 or res.satellite is None:
        return {}
    capital = res.capital
    core = res.curves["Core only"] / capital
    sat, sat_exp, core_exp = res.satellite.equity, res.satellite.exposure, res.core_exposure
    first = core.index[0]
    rows = {label: {"combined": [], "matched": []} for label, _ in windows(res)}
    for offset in range(days):
        anchor = first - pd.Timedelta(days=offset)  # reset schedule: offset days earlier
        parts = combine_sleeves_detail(core, sat, res.fraction, days, capital, anchor)
        comb_exp = (parts["core"] * core_exp + parts["satellite"] * sat_exp) / parts["total"]
        for label, start in windows(res):
            rows[label]["combined"].append(_stats(parts["total"], start))
            matched, _ = _matched_core(res.curves["Core only"], core_exp, comb_exp, days, capital, start, anchor)
            rows[label]["matched"].append(_stats(matched, start))
    return {"phases": days, "rows": rows}


def common_start(core_closes: dict[str, pd.Series], sma_days: list[int], scales=SCALES) -> pd.Timestamp:
    """First day on which every scaled variant has all its averages for every coin."""
    firsts = []
    for sc in scales:
        days = [max(2, round(d * sc)) for d in sma_days]
        for s in core_closes.values():
            w = trend_weights_series(pd.Series(s.to_numpy(dtype=float), index=_ns(s.index)).dropna(), days)
            firsts.append(w.first_valid_index())
    return max(f for f in firsts if f is not None)


def sma_scale_study(core_closes: dict[str, pd.Series], res: PortfolioBacktest, cfg: BotConfig,
                    scales=SCALES) -> dict:
    costs = Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate)
    start = common_start(core_closes, cfg.core.sma_days, scales)
    out = {"start": start, "variants": []}
    for sc in scales:
        days = [max(2, round(d * sc)) for d in cfg.core.sma_days]
        detail = simulate_core_detail(core_closes, replace(cfg.core, sma_days=days), costs, start_equity=res.capital)
        detail.index = _ns(detail.index)
        core = detail["equity"] / res.capital
        sat = (res.satellite.equity.reindex(core.index).ffill().fillna(1.0) if res.satellite is not None
               else pd.Series(1.0, index=core.index))
        combined = combine_sleeves_detail(core, sat, res.fraction, res.reset_days, res.capital)["total"]
        per_window = {}
        for label, wstart in windows(res, start):
            per_window[label] = {"core": _stats(core, wstart), "combined": _stats(combined, wstart)}
        out["variants"].append({"scale": sc, "days": days, "windows": per_window})
    return out


def _spread(values: list[float]) -> tuple[float, float, float]:
    v = np.array([x for x in values if x is not None and x == x])
    return (float(v.min()), float(np.median(v)), float(v.max())) if len(v) else (np.nan, np.nan, np.nan)


def format_core_robustness(res: PortfolioBacktest, phases: dict, scales: dict, cfg: BotConfig) -> str:
    lines = ["Core robustness - REPORTING ONLY: the spread from choices nobody validated. No variant is selected; "
             "the bot keeps its settings.", ""]
    if phases:
        lines += [f"1) Sleeve-reset timing: the {res.fraction:.0%}/{1 - res.fraction:.0%} account with its "
                  f"{phases['phases']}-day reset on each of the {phases['phases']} possible phases (live's phase is "
                  f"set by the day the core started, i.e. by chance)"]
        for label, r in phases["rows"].items():
            lines += [f"  {label}:",
                      f"    {'':<24}{'per year: min / median / max':>30}{'worst dip: shallowest / median / deepest':>44}"
                      f"{'Sharpe: min / median / max':>30}"]
            for who, name in (("combined", "65/35"), ("matched", "core at same exposure")):
                st = [x for x in r[who] if x is not None]
                if not st:
                    continue
                c, d, sh = (_spread([x[k] for x in st]) for k in ("cagr", "dip", "sharpe"))
                lines.append(f"    {name:<24}{c[0]:>+14.1f} / {c[1]:>+5.1f} / {c[2]:>+5.1f}%"
                             f"{-d[0]:>25.1f} / {-d[1]:>5.1f} / {-d[2]:>5.1f}%"
                             f"{sh[0]:>16.2f} / {sh[1]:>4.2f} / {sh[2]:>4.2f}")
            diffs = [(a["sharpe"] - b["sharpe"]) for a, b in zip(r["combined"], r["matched"]) if a and b]
            if diffs:
                lines.append(f"    65/35 minus core at same exposure, Sharpe: {min(diffs):+.2f} to {max(diffs):+.2f} "
                             f"across phases ({sum(x > 0 for x in diffs)} of {len(diffs)} phases above zero)")
        lines.append("")
    if scales:
        lines += [f"2) Trend lengths scaled (live uses {'/'.join(map(str, cfg.core.sma_days))} days). Compared from "
                  f"{scales['start']:%Y-%m-%d}, the first day every variant has all its averages:"]
        labels = list(scales["variants"][0]["windows"]) if scales["variants"] else []
        for label in labels:
            lines.append(f"  {label}:")
            lines.append(f"    {'':<28}{'core only: per year / worst dip / Sharpe':>44}"
                         f"{'65/35: per year / worst dip / Sharpe':>40}")
            for v in scales["variants"]:
                w = v["windows"][label]
                if not w["core"] or not w["combined"]:
                    continue
                name = f"{v['scale']:.1f}x ({'/'.join(map(str, v['days']))})" + ("  <- live" if v["scale"] == 1.0 else "")
                lines.append(f"    {name:<28}{w['core']['cagr']:>+20.1f}% / {-w['core']['dip']:>5.0f}% / "
                             f"{w['core']['sharpe']:>5.2f}{w['combined']['cagr']:>+16.1f}% / "
                             f"{-w['combined']['dip']:>5.0f}% / {w['combined']['sharpe']:>5.2f}")
            vals = [v["windows"][label] for v in scales["variants"] if v["windows"][label]["core"]]
            if len(vals) > 1:
                rng = lambda xs: max(xs) - min(xs)  # noqa: E731
                lines.append(f"    {'spread (max - min)':<28}{rng([x['core']['cagr'] for x in vals]):>20.1f}pts / "
                             f"{rng([x['core']['dip'] for x in vals]):>4.0f}pts / {rng([x['core']['sharpe'] for x in vals]):>5.2f}"
                             f"{rng([x['combined']['cagr'] for x in vals]):>15.1f}pts / "
                             f"{rng([x['combined']['dip'] for x in vals]):>4.0f}pts / "
                             f"{rng([x['combined']['sharpe'] for x in vals]):>5.2f}")
        lines.append("")
    lines += ["Read this as: how much of the core's result depends on arbitrary choices. A wide spread means the "
              "headline figures are one draw from a range; it is not a reason to pick the best variant."]
    return "\n".join(lines)
