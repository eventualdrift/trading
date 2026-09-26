"""Backtest the whole account: core sleeve + satellite sleeve, against holding BTC.

* Core: the BTC/ETH trend-ensemble allocation, simulated day by day with exactly the
  live rules (``core.simulate_core``).
* Satellite: the selected signal strategies' trades, sized from the satellite's own
  equity with the live risk rules (``portfolio_simulation``).
* The two sleeves compound separately and are reset to the target split every
  ``core.rebalance_sleeves_days`` - like the live bot.

Before the satellite's data starts it holds cash (0% return), so a long core history
isn't flattered or penalised by missing satellite data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .backtest.engine import Costs, Trade, portfolio_simulation
from .backtest.metrics import max_drawdown_pct
from .config import BotConfig
from .core import simulate_core


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


def satellite_daily(trades: list[Trade], cfg: BotConfig, index: pd.DatetimeIndex) -> pd.Series:
    """Satellite equity per day (start 1.0), from its trades with the live sizing rules."""
    if not trades:
        return pd.Series(1.0, index=index)
    r = cfg.risk
    curve, _ = portfolio_simulation(trades, risk_per_trade_pct=r.risk_per_trade_pct,
                                    max_position_pct=r.max_position_pct,
                                    max_open_positions=r.max_open_positions, start_equity=1.0)
    if curve.empty:
        return pd.Series(1.0, index=index)
    daily = curve.groupby(curve.index.floor("D")).last()
    return daily.reindex(index.union(daily.index)).ffill().reindex(index).fillna(1.0)


def combine_sleeves(core: pd.Series, satellite: pd.Series, fraction: float, reset_days: float,
                    capital: float) -> pd.Series:
    rc = core.pct_change().fillna(0.0).to_numpy()
    rs = satellite.pct_change().fillna(0.0).to_numpy()
    ec, es = capital * fraction, capital * (1 - fraction)
    last_reset = core.index[0]
    out = []
    for i, day in enumerate(core.index):
        ec *= 1 + rc[i]
        es *= 1 + rs[i]
        if reset_days > 0 and (day - last_reset).days >= reset_days:
            total = ec + es
            ec, es = total * fraction, total * (1 - fraction)
            last_reset = day
        out.append(ec + es)
    return pd.Series(out, index=core.index)


@dataclass
class PortfolioBacktest:
    capital: float
    fraction: float
    curves: dict[str, pd.Series] = field(default_factory=dict)
    since: pd.Timestamp | None = None
    satellite_trades: int = 0
    satellite_from: pd.Timestamp | None = None


def portfolio_backtest(core_closes: dict[str, pd.Series], sat_trades: list[Trade], cfg: BotConfig, *,
                       capital: float = 1000.0, fraction: float | None = None,
                       since: str | None = "2022-01-01") -> PortfolioBacktest:
    fraction = cfg.core.fraction if fraction is None else fraction
    costs = Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate)  # the core trades at market
    core = simulate_core(core_closes, cfg.core, costs, start_equity=capital) / capital
    index = core.index
    sat = satellite_daily(sat_trades, cfg, index)
    combined = combine_sleeves(core, sat, fraction, cfg.core.rebalance_sleeves_days, capital)
    closes = pd.DataFrame(core_closes).reindex(index).ffill()
    curves = {
        f"Combined ({fraction:.0%} core)": combined,
        "Core only": core * capital,
        "Satellite only": sat * capital,
    }
    btc = next((s for s in closes if s.startswith("BTC/")), None)
    if btc:
        curves["Hold BTC"] = closes[btc] / closes[btc].dropna().iloc[0] * capital
    if len(closes.columns) > 1:
        norm = closes.apply(lambda c: c / c.dropna().iloc[0])
        curves["Hold " + "+".join(s.split("/")[0] for s in closes)] = norm.mean(axis=1) * capital
    first_sat = min((t.entry_time for t in sat_trades), default=None)
    return PortfolioBacktest(capital, fraction, curves, pd.Timestamp(since, tz="UTC") if since else None,
                             len(sat_trades), first_sat)


def format_portfolio_backtest(res: PortfolioBacktest, quote: str = "USDT") -> str:
    def table(title: str, start: pd.Timestamp | None) -> list[str]:
        out = [title, f"  {'':<24}{'end value':>12}{'total':>10}{'per year':>10}{'worst dip':>11}{'Sharpe':>8}"]
        for name, curve in res.curves.items():
            c = curve.dropna()
            if start is not None:
                c = c[c.index >= start]
            if len(c) < 2:
                continue
            s = curve_stats(c)
            end_value = res.capital * c.iloc[-1] / c.iloc[0]
            out.append(f"  {name:<24}{end_value:>12,.0f}{s['total_pct']:>+9.0f}%{s['cagr_pct']:>+9.1f}%"
                       f"{-s['max_dd_pct']:>10.0f}%{s['sharpe']:>8.2f}")
        return out

    any_curve = next(iter(res.curves.values())).dropna()
    lines = [f"Whole-account backtest: {res.capital:,.0f} {quote}, {res.fraction:.0%} core / "
             f"{1 - res.fraction:.0%} satellite, fees and slippage included"]
    lines += table(f"\nFull history ({any_curve.index[0]:%Y-%m-%d} to {any_curve.index[-1]:%Y-%m-%d}):", None)
    if res.since is not None and res.since > any_curve.index[0]:
        lines += table(f"\nSince {res.since:%Y-%m-%d} (each rebased to {res.capital:,.0f}):", res.since)
    lines.append("\nYear by year (%):")
    names = list(res.curves)
    short = {n: n.replace(" core)", ")").replace("Combined (", "Combined ")[:15] for n in names}
    lines.append("  year  " + "".join(f"{short[n]:>16}" for n in names))
    per_year = {n: yearly_returns(c.dropna()) for n, c in res.curves.items()}
    for y in sorted(set().union(*[set(v.index) for v in per_year.values()])):
        lines.append(f"  {y}  " + "".join(
            f"{per_year[n].get(y, float('nan')):>+16.1f}" if y in per_year[n] else f"{'-':>16}" for n in names))
    sat_note = (f"from {res.satellite_from:%Y-%m-%d}" if res.satellite_from is not None else "none selected")
    lines += ["", f"Satellite: {res.satellite_trades} trades ({sat_note}); before that it holds cash.",
              "Past crypto cycles are not a forecast. Paper trading is the real test."]
    return "\n".join(lines)
