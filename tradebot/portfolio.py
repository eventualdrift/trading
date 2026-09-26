"""Backtest the whole account: core sleeve + satellite sleeve, against holding BTC.

* Core: the BTC/ETH trend-ensemble allocation, simulated day by day with exactly the
  live rules (``core.simulate_core``).
* Satellite: the selected signal strategies' trades, sized from the satellite's own
  equity with the live risk rules (same acceptance and sizing as ``portfolio_simulation``),
  and **marked to market every day** - open trades are valued at each daily close, so
  drawdowns and daily-return correlations include losses that were never realised.
* The two sleeves compound separately and are reset to the target split every
  ``core.rebalance_sleeves_days`` - like the live bot.

Before the satellite's data starts it holds cash (0% return), so a long core history
isn't flattered or penalised by missing satellite data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest.engine import Costs, Trade
from .backtest.metrics import max_drawdown_pct
from .config import BotConfig
from .core import simulate_core_detail


def _ns(index) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.as_unit("ns")


def curve_stats(equity: pd.Series) -> dict[str, float]:
    equity = equity.dropna()
    if len(equity) < 2:
        return {"total_pct": 0.0, "cagr_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0}
    rets = equity.pct_change().dropna()
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    growth = equity.iloc[-1] / equity.iloc[0]
    sd = rets.std()
    return {
        "total_pct": (growth - 1) * 100,
        "cagr_pct": (growth ** (1 / years) - 1) * 100 if growth > 0 else -100.0,
        "max_dd_pct": max_drawdown_pct(equity),
        "sharpe": float(rets.mean() / sd * math.sqrt(365)) if sd > 0 else 0.0,
    }


def yearly_returns(equity: pd.Series) -> pd.Series:
    ends = equity.groupby(equity.index.year).last()
    starts = pd.concat([pd.Series([equity.iloc[0]], index=[ends.index[0]]), ends.shift(1).dropna()])
    return (ends / starts.reindex(ends.index) - 1) * 100


# ------------------------------------------------------------------ satellite
@dataclass
class SatelliteRun:
    equity: pd.Series  # daily, marked to market, starts at 1.0
    exposure: pd.Series  # open notional (at market) / equity, daily
    open_count: pd.Series  # open positions, daily
    taken: list[Trade] = field(default_factory=list)
    skipped: dict[str, list[Trade]] = field(default_factory=dict)  # reason -> trades
    sizes: list[float] = field(default_factory=list)  # entry notional / equity, per taken trade
    realized_end: float = 1.0  # same number portfolio_simulation gives
    unmarked: int = 0  # taken trades without daily prices (valued only when closed)


def simulate_satellite(trades: list[Trade], cfg: BotConfig, index: pd.DatetimeIndex,
                       closes: dict[str, pd.Series] | None = None) -> SatelliteRun:
    """Replay the satellite's trades like the live account and mark open ones to market daily."""
    r = cfg.risk
    index = _ns(index)
    n = len(index)
    closes = closes or {}
    entry_fee = cfg.costs_model().entry_fee
    equity = 1.0
    open_: list[tuple[Trade, float, float]] = []  # (trade, notional, pnl)
    booked: list[tuple[Trade, float, float]] = []
    skipped: dict[str, list[Trade]] = {}
    sizes = []
    for t in sorted(trades, key=lambda t: (t.entry_time, t.symbol)):
        still = []
        for item in sorted(open_, key=lambda x: x[0].exit_time):
            if item[0].exit_time <= t.entry_time:
                equity += item[2]
            else:
                still.append(item)
        open_ = still
        if len(open_) >= r.max_open_positions:
            skipped.setdefault(f"max open positions ({r.max_open_positions})", []).append(t)
            continue
        if any(ot.symbol == t.symbol for ot, _, _ in open_):
            skipped.setdefault("already holding that coin", []).append(t)
            continue
        frac = min(r.risk_per_trade_pct / 100.0 / max(t.stop_pct, 1e-9), r.max_position_pct / 100.0)
        notional = equity * frac
        item = (t, notional, notional * t.return_pct)
        open_.append(item)
        booked.append(item)
        sizes.append(frac)
    realized_end = equity + sum(pnl for _, _, pnl in open_)

    days = index.floor("D")
    realized = np.zeros(n)
    unreal = np.zeros(n)
    gross = np.zeros(n)
    count = np.zeros(n)
    unmarked = 0
    price_cache: dict[str, pd.Series] = {}
    for t, notional, pnl in booked:
        d_in = days.searchsorted(pd.Timestamp(t.entry_time).tz_convert("UTC").floor("D"))
        d_out = days.searchsorted(pd.Timestamp(t.exit_time).tz_convert("UTC").floor("D"))
        if d_out < n:
            realized[d_out] += pnl
        if d_out <= d_in:
            continue
        if t.symbol not in price_cache:
            c = closes.get(t.symbol)
            price_cache[t.symbol] = (pd.Series(c.to_numpy(dtype=float), index=_ns(c.index).floor("D"))
                                     .groupby(level=0).last().reindex(days, method="ffill")
                                     if c is not None and len(c) else None)
        px = price_cache[t.symbol]
        sign = 1.0 if t.side == "long" else -1.0
        span = slice(d_in, min(d_out, n))
        count[span] += 1
        if px is None:
            unmarked += 1
            gross[span] += notional
            continue
        path = px.to_numpy()[span]
        path = np.where(np.isnan(path), t.entry_price, path)
        unreal[span] += notional * (sign * (path / t.entry_price - 1.0) - entry_fee)
        gross[span] += notional * path / t.entry_price
    eq = 1.0 + np.cumsum(realized) + unreal
    return SatelliteRun(
        equity=pd.Series(eq, index=index),
        exposure=pd.Series(np.divide(gross, eq, out=np.zeros(n), where=eq > 0), index=index),
        open_count=pd.Series(count, index=index),
        taken=[t for t, _, _ in booked], skipped=skipped, sizes=sizes,
        realized_end=realized_end, unmarked=unmarked,
    )


def satellite_daily(trades: list[Trade], cfg: BotConfig, index: pd.DatetimeIndex,
                    closes: dict[str, pd.Series] | None = None) -> pd.Series:
    """Satellite equity per day (start 1.0), marked to market where prices are given."""
    return simulate_satellite(trades, cfg, index, closes).equity


def combine_sleeves_detail(core: pd.Series, satellite: pd.Series, fraction: float, reset_days: float,
                           capital: float) -> pd.DataFrame:
    rc = core.pct_change().fillna(0.0).to_numpy()
    rs = satellite.pct_change().fillna(0.0).to_numpy()
    ec, es = capital * fraction, capital * (1 - fraction)
    last_reset = core.index[0]
    rows = []
    for i, day in enumerate(core.index):
        ec *= 1 + rc[i]
        es *= 1 + rs[i]
        if reset_days > 0 and (day - last_reset).days >= reset_days:
            total = ec + es
            ec, es = total * fraction, total * (1 - fraction)
            last_reset = day
        rows.append((ec + es, ec, es))
    return pd.DataFrame(rows, index=core.index, columns=["total", "core", "satellite"])


def combine_sleeves(core: pd.Series, satellite: pd.Series, fraction: float, reset_days: float,
                    capital: float) -> pd.Series:
    return combine_sleeves_detail(core, satellite, fraction, reset_days, capital)["total"]


# ------------------------------------------------------------------ report
def combo_split_stats(key: str, is_trades: list[Trade], oos_trades: list[Trade]) -> dict:
    def part(ts):
        if not ts:
            return {"trades": 0, "exp_r": None, "start": None, "end": None}
        return {"trades": len(ts), "exp_r": float(np.mean([t.r_multiple for t in ts])),
                "start": min(t.entry_time for t in ts), "end": max(t.entry_time for t in ts)}
    return {"key": key, "is": part(is_trades), "oos": part(oos_trades)}


@dataclass
class PortfolioBacktest:
    capital: float
    fraction: float
    curves: dict[str, pd.Series] = field(default_factory=dict)
    since: pd.Timestamp | None = None
    satellite_trades: int = 0
    satellite_from: pd.Timestamp | None = None
    satellite: SatelliteRun | None = None
    core_exposure: pd.Series | None = None
    combined_exposure: pd.Series | None = None
    combos: list[dict] = field(default_factory=list)
    universe: dict | None = None
    assumptions: str = ""
    sizing: str = ""
    reset_days: float = 30


def _matched_core(core: pd.Series, core_exp: pd.Series, target_exp: pd.Series, reset_days: float,
                  capital: float, start: pd.Timestamp | None) -> tuple[pd.Series, float]:
    """Core-only held at the combined account's average exposure (the rest in cash)."""
    if start is not None:
        core, core_exp, target_exp = (x[x.index >= start] for x in (core, core_exp, target_exp))
    avg_core = float(core_exp.mean())
    k = min(float(target_exp.mean()) / avg_core, 1.0) if avg_core > 0 else 0.0
    cash = pd.Series(1.0, index=core.index)
    return combine_sleeves(core / core.iloc[0], cash, k, reset_days, capital), k


def portfolio_backtest(core_closes: dict[str, pd.Series], sat_trades: list[Trade], cfg: BotConfig, *,
                       capital: float = 1000.0, fraction: float | None = None,
                       since: str | None = "2022-01-01", sat_closes: dict[str, pd.Series] | None = None,
                       combos: list[dict] | None = None, universe: dict | None = None) -> PortfolioBacktest:
    fraction = cfg.core.fraction if fraction is None else fraction
    costs = Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate)  # the core trades at market
    detail = simulate_core_detail(core_closes, cfg.core, costs, start_equity=capital)
    detail.index = _ns(detail.index)
    core = detail["equity"] / capital
    core_exp = (detail["invested"] / detail["equity"]).fillna(0.0)
    index = core.index
    sat = simulate_satellite(sat_trades, cfg, index, sat_closes)
    parts = combine_sleeves_detail(core, sat.equity, fraction, cfg.core.rebalance_sleeves_days, capital)
    combined_exp = (parts["core"] * core_exp + parts["satellite"] * sat.exposure) / parts["total"]
    closes = pd.DataFrame({k: pd.Series(v.to_numpy(dtype=float), index=_ns(v.index)) for k, v in core_closes.items()})
    closes = closes.groupby(level=0).last().reindex(index).ffill()
    curves = {
        f"Combined ({fraction:.0%} core)": parts["total"],
        "Core only": core * capital,
        "Satellite only": sat.equity * capital,
    }
    btc = next((s for s in closes if s.startswith("BTC/")), None)
    if btc:
        curves["Hold BTC"] = closes[btc] / closes[btc].dropna().iloc[0] * capital
    if len(closes.columns) > 1:
        norm = closes.apply(lambda c: c / c.dropna().iloc[0])
        curves["Hold " + "+".join(s.split("/")[0] for s in closes)] = norm.mean(axis=1) * capital
    first_sat = min((t.entry_time for t in sat_trades), default=None)
    r = cfg.risk
    sizing = (f"{r.risk_per_trade_pct:g}% of satellite equity at risk per trade (size = risk / stop distance), "
              f"max {r.max_position_pct:g}% per position, max {r.max_open_positions} open, one per coin")
    return PortfolioBacktest(capital, fraction, curves, pd.Timestamp(since, tz="UTC") if since else None,
                             len(sat_trades), first_sat, sat, core_exp, combined_exp, combos or [], universe,
                             cfg.costs_description(), sizing, cfg.core.rebalance_sleeves_days)


def _corr(a: pd.Series, b: pd.Series, rule: str | None = None) -> tuple[float | None, int]:
    if rule:
        a, b = a.resample(rule).last(), b.resample(rule).last()
    ra, rb = a.pct_change(), b.pct_change()
    both = pd.concat([ra, rb], axis=1).dropna()
    if len(both) < 10 or both.iloc[:, 0].std() == 0 or both.iloc[:, 1].std() == 0:
        return None, len(both)
    return float(both.iloc[:, 0].corr(both.iloc[:, 1])), len(both)


def _day(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d") if ts is not None else "-"


def format_satellite_measurement(res: PortfolioBacktest) -> list[str]:
    sat = res.satellite
    if sat is None or not res.satellite_trades:
        return ["", "Satellite measurement: no satellite trades (no strategy selected yet)."]
    start = pd.Timestamp(res.satellite_from)
    active = sat.equity.index >= start.floor("D")
    taken, skipped = sat.taken, [t for ts in sat.skipped.values() for t in ts]
    everything = taken + skipped

    def exp(ts):
        return f"{np.mean([t.r_multiple for t in ts]):+.3f}R ({len(ts)})" if ts else "- (0)"

    out = ["", f"Satellite measurement (from its first trade, {_day(start)}):",
           f"  Candidate trades: {len(everything)} -> taken {len(taken)} ({len(taken) / len(everything):.0%}), "
           f"skipped {len(skipped)}" + "".join(f"; {len(v)} {k}" for k, v in sat.skipped.items()),
           f"  R per trade (trade count): all candidates {exp(everything)} · taken {exp(taken)} · "
           f"skipped {exp(skipped)}"]
    if sat.sizes:
        s = np.array(sat.sizes) * 100
        out.append(f"  Position size at entry (% of satellite equity): mean {s.mean():.1f}%, median "
                   f"{np.median(s):.1f}%, min {s.min():.1f}%, max {s.max():.1f}%")
        out.append(f"    sizing: {res.sizing}")
    e, c = sat.exposure[active], sat.open_count[active]
    out.append(f"  Exposure (open positions at market / satellite equity): average {e.mean():.0%}, "
               f"median {e.median():.0%}, max {e.max():.0%}; days with a position {(c > 0).mean():.0%}; "
               f"average open positions {c.mean():.1f}")
    if sat.unmarked:
        out.append(f"  ! {sat.unmarked} trades had no daily prices and are valued only when closed")
    out.append(f"  Marked-to-market end value matches the realised replay: "
               f"{sat.equity.iloc[-1]:.4f} vs {sat.realized_end:.4f} (x start)")
    if res.combos:
        out.append("  Per strategy - the trades behind each R figure (in-sample / out-of-sample split is per coin):")
        for cst in res.combos:
            parts = []
            for label in ("is", "oos"):
                p = cst[label]
                parts.append(f"{label.upper():<3} {p['trades']:>4} trades "
                             + (f"{p['exp_r']:+.3f}R ({_day(p['start'])} to {_day(p['end'])})" if p["trades"] else ""))
            out.append(f"    {cst['key']:<16} " + " | ".join(parts))
    core = res.curves["Core only"][active]
    satc = sat.equity[active]
    d, nd = _corr(core, satc)
    w, nw = _corr(core, satc, "W")
    btc = res.curves.get("Hold BTC")
    b, _ = _corr(btc[active], satc) if btc is not None else (None, 0)
    fmt = (lambda v: f"{v:+.2f}" if v is not None else "n/a")
    out.append(f"  Correlation, core vs satellite returns (both active): daily {fmt(d)} ({nd} days), "
               f"weekly {fmt(w)} ({nw} weeks); satellite vs BTC daily {fmt(b)}")
    u = res.universe
    if u:
        syms = u.get("symbols", [])
        first = {s: pd.Timestamp(v) for s, v in (u.get("first") or {}).items()}
        line = (f"  Universe: {u.get('source', 'today')} - {len(syms)} coins, NOT the coins listed at the time. "
                f"Coins that fell out of the top or were delisted are missing (survivorship bias - it "
                f"flatters the satellite).")
        out.append(line)
        if first:
            earliest = min(first.values())
            late = sorted((v, s) for s, v in first.items() if v > earliest + pd.Timedelta(days=2))
            out.append(f"    {len(first) - len(late)} of {len(first)} have data from {_day(earliest)}; "
                       f"{len(late)} start later" + (": " + ", ".join(f"{s.split('/')[0]} {_day(v)}" for v, s in late[:12])
                                                     + (" ..." if len(late) > 12 else "") if late else ""))
    out.append("  The candidate trades include the period used to select these strategies, so the satellite's "
               "results are optimistic.")
    return out


def format_portfolio_backtest(res: PortfolioBacktest, quote: str = "USDT") -> str:
    matched_name = "Core at same exposure"

    def table(title: str, start: pd.Timestamp | None) -> list[str]:
        out = [title, f"  {'':<28}{'end value':>12}{'total':>10}{'per year':>10}{'worst dip':>11}{'Sharpe':>8}"
                      f"{'exposure':>10}"]
        exposure = {f"Combined ({res.fraction:.0%} core)": res.combined_exposure, "Core only": res.core_exposure,
                    "Satellite only": res.satellite.exposure if res.satellite is not None else None}
        rows = list(res.curves.items())
        if res.core_exposure is not None and res.combined_exposure is not None:
            core_c = res.curves["Core only"]
            m, k = _matched_core(core_c, res.core_exposure, res.combined_exposure,
                                 res.reset_days, res.capital, start)
            rows.insert(2, (f"{matched_name} ({k:.0%})", m))
            exposure[rows[2][0]] = res.core_exposure * k
        for name, curve in rows:
            c = curve.dropna()
            if start is not None:
                c = c[c.index >= start]
            if len(c) < 2:
                continue
            s = curve_stats(c)
            end_value = res.capital * c.iloc[-1] / c.iloc[0]
            ex = exposure.get(name)
            ex_txt = f"{ex[ex.index >= start].mean() if start is not None else ex.mean():>9.0%}" \
                if ex is not None and len(ex) else f"{'-':>9}"
            out.append(f"  {name:<28}{end_value:>12,.0f}{s['total_pct']:>+9.0f}%{s['cagr_pct']:>+9.1f}%"
                       f"{-s['max_dd_pct']:>10.0f}%{s['sharpe']:>8.2f} {ex_txt}")
        return out

    any_curve = next(iter(res.curves.values())).dropna()
    lines = [f"Whole-account backtest: {res.capital:,.0f} {quote}, {res.fraction:.0%} core / "
             f"{1 - res.fraction:.0%} satellite, fees and slippage included"]
    if res.assumptions:
        lines.append(f"Satellite {res.assumptions[0].lower()}{res.assumptions[1:]} Core: market orders "
                     f"(taker fee + slippage). Satellite marked to market daily.")
    lines += table(f"\nFull history ({any_curve.index[0]:%Y-%m-%d} to {any_curve.index[-1]:%Y-%m-%d}):", None)
    if res.since is not None and res.since > any_curve.index[0]:
        lines += table(f"\nSince {res.since:%Y-%m-%d} (each rebased to {res.capital:,.0f}):", res.since)
    lines.append(f"\n'{matched_name}' holds the core at the combined account's average exposure (the rest in "
                 f"cash): what 65/35 must beat on Sharpe or worst dip for the satellite to add anything.")
    lines.append("\nYear by year (%):")
    names = list(res.curves)
    short = {n: (f"Combined {res.fraction:.0%}" if n.startswith("Combined") else n)[:15] for n in names}
    lines.append("  year  " + "".join(f"{short[n]:>16}" for n in names))
    per_year = {n: yearly_returns(c.dropna()) for n, c in res.curves.items()}
    for y in sorted(set().union(*[set(v.index) for v in per_year.values()])):
        lines.append(f"  {y}  " + "".join(
            f"{per_year[n].get(y, float('nan')):>+16.1f}" if y in per_year[n] else f"{'-':>16}" for n in names))
    sat_note = (f"from {res.satellite_from:%Y-%m-%d}" if res.satellite_from is not None else "none selected")
    lines += ["", f"Satellite: {res.satellite_trades} candidate trades ({sat_note}); before that it holds cash, "
                  f"so full-history figures before then show 65% core + 35% cash."]
    lines += format_satellite_measurement(res)
    lines.append("\nPast crypto cycles are not a forecast. Paper trading is the real test.")
    return "\n".join(lines)
