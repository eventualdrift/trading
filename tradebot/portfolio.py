"""Backtest the whole account: core sleeve + satellite sleeve, against holding BTC.

* Core: the BTC/ETH trend-ensemble allocation, simulated day by day with exactly the
  live rules (``core.simulate_core``).
* Satellite: every signal of the selected strategies is a candidate, taken as live takes it
  (coin not already held, a slot free once a position is flat), sized from the satellite's own
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

from .backtest.engine import Costs, Trade, flat_at, live_order
from .backtest.metrics import max_drawdown_pct
from .config import BotConfig
from .core import simulate_core_detail
from .risk import position_fraction


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


def worst_dip(equity: pd.Series) -> tuple[float, pd.Timestamp | None, pd.Timestamp | None]:
    """(worst drawdown %, the peak before it, its low)."""
    e = equity.dropna()
    if len(e) < 2:
        return 0.0, None, None
    peak = e.cummax()
    dd = e / peak - 1.0
    low = dd.idxmin()
    top = e.loc[:low].idxmax()
    return float(-dd.min() * 100.0), top, low


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
    # one entry per trade skipped because the slots/budget were full: the strategies holding the
    # slots then, and whether it lost at the same close (it would have fit without the trades
    # taken at that very moment, i.e. it was out-ranked) or found them already full
    skip_detail: list[dict] = field(default_factory=list)


def simulate_satellite(trades: list[Trade], cfg: BotConfig, index: pd.DatetimeIndex,
                       closes: dict[str, pd.Series] | None = None,
                       open_risk_pct: float | None | str = "config") -> SatelliteRun:
    """Replay the satellite's trades like the live account and mark open ones to market daily.

    ``open_risk_pct``: the open-risk budget rule instead of max_open_positions (default: whatever
    risk.max_open_risk_pct says). Same acceptance and sizing as the live risk manager."""
    r = cfg.risk
    budget = r.max_open_risk_pct if open_risk_pct == "config" else open_risk_pct
    index = _ns(index)
    n = len(index)
    closes = closes or {}
    entry_fee = cfg.costs_model().entry_fee
    equity = 1.0
    open_: list[tuple[Trade, float, float]] = []  # (trade, notional, pnl)
    booked: list[tuple[Trade, float, float]] = []
    skipped: dict[str, list[Trade]] = {}
    skip_detail: list[dict] = []
    sizes = []

    def key(tr: Trade) -> str:
        return f"{tr.strategy}@{tr.timeframe}"

    for t in sorted(trades, key=live_order):
        still = []
        for item in sorted(open_, key=lambda x: flat_at(x[0])):
            if flat_at(item[0]) <= t.entry_time:  # the slot is free only once the position is flat
                equity += item[2]
            else:
                still.append(item)
        open_ = still
        frac = position_fraction(t.stop_pct, r)

        def full(holding) -> bool:
            if budget is None:
                return len(holding) >= r.max_open_positions
            at_stake = sum(n_ * ot.stop_pct for ot, n_, _ in holding
                           if ot.protected_time is None or ot.protected_time > t.entry_time)
            return at_stake + equity * frac * t.stop_pct > equity * budget / 100.0 + 1e-12

        if full(open_):
            reason = (f"max open positions ({r.max_open_positions})" if budget is None
                      else f"open-risk budget ({budget:g}%)")
            skipped.setdefault(reason, []).append(t)
            earlier = [x for x in open_ if x[0].entry_time != t.entry_time]
            skip_detail.append({"trade": t, "strategy": key(t), "same_close": not full(earlier),
                                "holders": [key(ot) for ot, _, _ in open_],
                                "winners": [key(ot) for ot, _, _ in open_ if ot.entry_time == t.entry_time]})
            continue
        if any(ot.symbol == t.symbol for ot, _, _ in open_):
            skipped.setdefault("already holding that coin", []).append(t)
            continue
        room = equity * r.max_total_exposure_pct / 100.0 - sum(n_ for _, n_, _ in open_)
        if room <= 0:
            skipped.setdefault("total exposure limit", []).append(t)
            continue
        notional = min(equity * frac, room)  # like the live risk manager: shrink to fit
        frac = notional / equity
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
        realized_end=realized_end, unmarked=unmarked, skip_detail=skip_detail,
    )


def satellite_daily(trades: list[Trade], cfg: BotConfig, index: pd.DatetimeIndex,
                    closes: dict[str, pd.Series] | None = None) -> pd.Series:
    """Satellite equity per day (start 1.0), marked to market where prices are given."""
    return simulate_satellite(trades, cfg, index, closes).equity


def _last_scheduled_reset(first: pd.Timestamp, anchor: pd.Timestamp | None, reset_days: float) -> pd.Timestamp:
    """The reset schedule is anchor, anchor + N days, ...: the last scheduled reset on or before ``first``."""
    if anchor is None or reset_days <= 0:
        return first
    periods = (first - anchor).days // int(reset_days)
    return anchor + pd.Timedelta(days=int(periods * int(reset_days)))


def combine_sleeves_detail(core: pd.Series, satellite: pd.Series, fraction: float, reset_days: float,
                           capital: float, anchor: pd.Timestamp | None = None) -> pd.DataFrame:
    """Two sleeves compounding separately, reset to the split every ``reset_days``. ``anchor`` fixes
    the reset schedule in calendar time (default: the first day), so windows share one schedule."""
    rc = core.pct_change().fillna(0.0).to_numpy()
    rs = satellite.pct_change().fillna(0.0).to_numpy()
    ec, es = capital * fraction, capital * (1 - fraction)
    last_reset = _last_scheduled_reset(core.index[0], anchor, reset_days)
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
                    capital: float, anchor: pd.Timestamp | None = None) -> pd.Series:
    return combine_sleeves_detail(core, satellite, fraction, reset_days, capital, anchor)["total"]


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


def account_stats(res: "PortfolioBacktest", start: pd.Timestamp | None) -> dict[str, dict]:
    """Stats of the combined account, core only, core at the same exposure and satellite only."""
    def stats(curve: pd.Series, exposure: pd.Series | None) -> dict:
        c = curve.dropna()
        if start is not None:
            c = c[c.index >= start]
        out = curve_stats(c) if len(c) > 1 else {"total_pct": 0.0, "cagr_pct": 0.0, "max_dd_pct": 0.0, "sharpe": 0.0}
        if exposure is not None and len(exposure):
            e = exposure[exposure.index >= start] if start is not None else exposure
            out["exposure"] = float(e.mean())
        return out

    combined_name = f"Combined ({res.fraction:.0%} core)"
    matched, k = _matched_core(res.curves["Core only"], res.core_exposure, res.combined_exposure,
                               res.reset_days, res.capital, start)
    return {
        "combined": stats(res.curves[combined_name], res.combined_exposure),
        "core": stats(res.curves["Core only"], res.core_exposure),
        "matched": {**stats(matched, res.core_exposure * k), "k": k},
        "satellite": stats(res.curves["Satellite only"], res.satellite.exposure if res.satellite else None),
    }


def _matched_core(core: pd.Series, core_exp: pd.Series, target_exp: pd.Series, reset_days: float,
                  capital: float, start: pd.Timestamp | None,
                  anchor: pd.Timestamp | None = None) -> tuple[pd.Series, float]:
    """Core-only held at the combined account's average exposure (the rest in cash)."""
    if start is not None:
        core, core_exp, target_exp = (x[x.index >= start] for x in (core, core_exp, target_exp))
    avg_core = float(core_exp.mean())
    k = min(float(target_exp.mean()) / avg_core, 1.0) if avg_core > 0 else 0.0
    cash = pd.Series(1.0, index=core.index)
    return combine_sleeves(core / core.iloc[0], cash, k, reset_days, capital, anchor), k


def portfolio_backtest(core_closes: dict[str, pd.Series], sat_trades: list[Trade], cfg: BotConfig, *,
                       capital: float = 1000.0, fraction: float | None = None,
                       since: str | None = "2022-01-01", sat_closes: dict[str, pd.Series] | None = None,
                       combos: list[dict] | None = None, universe: dict | None = None,
                       open_risk_pct: float | None | str = "config") -> PortfolioBacktest:
    fraction = cfg.core.fraction if fraction is None else fraction
    budget = cfg.risk.max_open_risk_pct if open_risk_pct == "config" else open_risk_pct
    costs = Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate)  # the core trades at market
    detail = simulate_core_detail(core_closes, cfg.core, costs, start_equity=capital)
    detail.index = _ns(detail.index)
    core = detail["equity"] / capital
    core_exp = (detail["invested"] / detail["equity"]).fillna(0.0)
    index = core.index
    sat = simulate_satellite(sat_trades, cfg, index, sat_closes, open_risk_pct=budget)
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
    slots = (f"open-risk budget {budget:g}% (risk still at stake; 0 once a stop is at entry)" if budget is not None
             else f"max {r.max_open_positions} open")
    sizing = (f"{r.risk_per_trade_pct:g}% of satellite equity at risk per trade (size = risk / stop distance), "
              f"max {r.max_position_pct:g}% per position, {slots}, one per coin, "
              f"total at most {r.max_total_exposure_pct:g}% of the satellite")
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


def _universe_lines(res: "PortfolioBacktest") -> list[str]:
    out: list[str] = []
    u = res.universe
    if u:
        syms = u.get("symbols", [])
        first = {s: pd.Timestamp(v) for s, v in (u.get("first") or {}).items()}
        line = (f"  Universe: {u.get('source', 'today')} - {len(syms)} coins, NOT the coins listed at the time. "
                f"Coins that fell out of the top or were delisted are missing (survivorship bias - it "
                f"flatters the satellite).")
        out.append(line)
        out.append("    Coins: " + ", ".join(s.split("/")[0] for s in syms))
        if u.get("selection_now"):
            made = u.get("selection_created")
            made_txt = f" (selected by learn on {pd.Timestamp(made, unit='s', tz='UTC'):%Y-%m-%d %H:%M} UTC)" if made else ""
            out.append(f"    Strategies: {', '.join(u['selection_now'])}{made_txt}")
            if u.get("frozen") and u.get("selection_source"):
                out.append(f"    Strategy selection used: {u['selection_source']}")
                today = u.get("selection_today") or []
                if today and sorted(today) != sorted(u["selection_now"]):
                    out.append(f"    (today's selection is {', '.join(today)}; --current-selection reruns with it)")
        if u.get("data_end_ms"):
            out.append(f"    Data to {pd.Timestamp(u['data_end_ms'], unit='ms', tz='UTC'):%Y-%m-%d %H:%M} UTC"
                       + (" (frozen: from the universe file)" if u.get("frozen") else ""))
        if u.get("file"):
            out.append(f"    Reproduce this run: --universe-file {u['file']}")
        if u.get("code"):
            from .provenance import config_summary

            out.append(f"    Code: {u['code']} · settings {u.get('config_hash')}: {config_summary(u.get('config') or {})}")
        if u.get("stamped_file"):
            out.append(f"    Same coins and data end with this code and settings: --universe-file {u['stamped_file']}")
        if u.get("frozen"):
            saved_code, saved_hash = u.get("saved_code"), u.get("saved_config_hash")
            if saved_code is None and saved_hash is None:
                out.append("    ! The universe file predates code/settings recording: it can't confirm this rerun "
                           "used the same code and settings as the original run.")
            else:
                if saved_code != u.get("code"):
                    out.append(f"    ! Different code from the saved run ({saved_code} then, {u.get('code')} now): "
                               f"numbers can differ for that reason alone.")
                for d in u.get("config_diffs") or []:
                    out.append(f"    ! Setting changed since the saved run - {d}")
        if first:
            earliest = min(first.values())
            late = sorted((v, s) for s, v in first.items() if v > earliest + pd.Timedelta(days=2))
            out.append(f"    {len(first) - len(late)} of {len(first)} have data from {_day(earliest)}; "
                       f"{len(late)} start later" + (": " + ", ".join(f"{s.split('/')[0]} {_day(v)}" for v, s in late[:12])
                                                     + (" ..." if len(late) > 12 else "") if late else ""))
    return out


def format_satellite_measurement(res: PortfolioBacktest) -> list[str]:
    sat = res.satellite
    if sat is None or not res.satellite_trades:
        return ["", "Satellite measurement: no satellite trades (no strategy selected yet)."] + _universe_lines(res)
    start = pd.Timestamp(res.satellite_from)
    active = sat.equity.index >= start.floor("D")
    taken, skipped = sat.taken, [t for ts in sat.skipped.values() for t in ts]
    everything = taken + skipped
    # a coin already held: the same move signalled again, not an opportunity lost to the slot limit
    held = {id(t) for t in sat.skipped.get("already holding that coin", [])}
    lost = [t for t in skipped if id(t) not in held]

    def exp(ts):
        return f"{np.mean([t.r_multiple for t in ts]):+.3f}R ({len(ts)})" if ts else "- (0)"

    out = ["", f"Satellite measurement (from its first trade, {_day(start)}):",
           f"  Candidate trades (every signal): {len(everything)} -> taken {len(taken)} "
           f"({len(taken) / len(everything):.0%}), skipped {len(skipped)}"
           + "".join(f"; {len(v)} {k}" for k, v in sat.skipped.items()),
           f"  R per trade (trade count): taken {exp(taken)} · skipped for lack of room {exp(lost)}"
           + (f" · skipped as the coin was already held {exp([t for t in skipped if id(t) in held])}" if held else "")]
    keys = sorted({f"{t.strategy}@{t.timeframe}" for t in everything})
    if len(keys) > 1:  # which strategies got the slots
        out.append("  By strategy (signals -> taken; R of taken / skipped for lack of room):")
        for k in keys:
            tk = [t for t in taken if f"{t.strategy}@{t.timeframe}" == k]
            sk = [t for t in lost if f"{t.strategy}@{t.timeframe}" == k]
            n = sum(f"{t.strategy}@{t.timeframe}" == k for t in everything)
            out.append(f"    {k:<16} {n:>5} -> {len(tk):>4} ({len(tk) / max(n, 1):.0%}); "
                       f"taken {exp(tk)} · skipped {exp(sk)}")
    if sat.skip_detail:
        out.append("  Why slots were full (per skipped strategy): 'same close' = it would have fit but "
                   "higher-ranked signals at the same close took the slots; 'already full' = slots were "
                   "held by earlier trades:")
        for k in sorted({d["strategy"] for d in sat.skip_detail}):
            ds = [d for d in sat.skip_detail if d["strategy"] == k]
            same = [d for d in ds if d["same_close"]]
            full_ = [d for d in ds if not d["same_close"]]

            def shares(lists) -> str:
                counts: dict[str, int] = {}
                for lst in lists:
                    for h in lst:
                        counts[h] = counts.get(h, 0) + 1
                total = sum(counts.values()) or 1
                return ", ".join(f"{h} {c / total:.0%}" for h, c in sorted(counts.items(), key=lambda kv: -kv[1]))

            out.append(f"    {k:<16} {len(ds):>5} skipped: {len(same)} same close"
                       + (f" (won by {shares([d['winners'] for d in same])})" if same else "")
                       + f"; {len(full_)} already full"
                       + (f" (slots held by {shares([d['holders'] for d in full_])})" if full_ else ""))
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
    gap = sat.equity.iloc[-1] - sat.realized_end
    out.append(f"  End value, marked to market vs realised replay: {sat.equity.iloc[-1]:.4f} vs {sat.realized_end:.4f} "
               f"(x start)" + ("" if abs(gap) < 1e-9 else
                               " - the difference is trades still open or closing after the last daily close"))
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
    out += _universe_lines(res)
    out.append("  The candidate trades include the period used to select these strategies, so the satellite's "
               "results are optimistic.")
    return out


def format_portfolio_backtest(res: PortfolioBacktest, quote: str = "USDT") -> str:
    matched_name = "Core at same exposure"
    combined_name = f"Combined ({res.fraction:.0%} core)"

    sat_first = None
    if res.satellite_from is not None:
        sat_first = pd.Timestamp(res.satellite_from)
        sat_first = (sat_first.tz_localize("UTC") if sat_first.tz is None else sat_first.tz_convert("UTC")).floor("D")

    def table(title: str, start: pd.Timestamp | None, measures_satellite: bool = False) -> list[str]:
        out = [title, f"  {'':<28}{'end value':>12}{'total':>10}{'per year':>10}{'worst dip':>11}{'dip low':>12}"
                      f"{'Sharpe':>8}{'exposure':>10}"]
        exposure = {combined_name: res.combined_exposure, "Core only": res.core_exposure,
                    "Satellite only": res.satellite.exposure if res.satellite is not None else None}
        rows = list(res.curves.items())
        if res.core_exposure is not None and res.combined_exposure is not None:
            core_c = res.curves["Core only"]
            m, k = _matched_core(core_c, res.core_exposure, res.combined_exposure,
                                 res.reset_days, res.capital, start)
            rows.insert(2, (f"{matched_name} ({k:.0%})", m))
            exposure[rows[2][0]] = res.core_exposure * k
        seen = {}
        for name, curve in rows:
            c = curve.dropna()
            if start is not None:
                c = c[c.index >= start]
            if len(c) < 2:
                continue
            s = curve_stats(c)
            dip, top, low = worst_dip(c)
            seen[name] = (s, dip, top, low)
            end_value = res.capital * c.iloc[-1] / c.iloc[0]
            ex = exposure.get(name)
            ex_txt = f"{ex[ex.index >= start].mean() if start is not None else ex.mean():>9.0%}" \
                if ex is not None and len(ex) else f"{'-':>9}"
            low_txt = f"{low:%Y-%m-%d}" if low is not None else "-"
            out.append(f"  {name:<28}{end_value:>12,.0f}{s['total_pct']:>+9.0f}%{s['cagr_pct']:>+9.1f}%"
                       f"{-dip:>10.0f}%{low_txt:>12}{s['sharpe']:>8.2f} {ex_txt}")
        matched_key = next((n for n in seen if n.startswith(matched_name)), None)
        if combined_name in seen and matched_key:
            (cs, cdip, ctop, clow), (ms, mdip, mtop, mlow) = seen[combined_name], seen[matched_key]
            sharpe_ok, dip_ok = cs["sharpe"] > ms["sharpe"], cdip < mdip
            margin_ok = cs["sharpe"] >= ms["sharpe"] + 0.10 or cdip <= mdip - 3.0
            verdict = ("beats it on Sharpe and worst dip" if sharpe_ok and dip_ok else
                       "beats it on Sharpe only" if sharpe_ok else
                       "beats it on worst dip only" if dip_ok else "does not beat it")
            out.append(f"  Rule (65/35 must beat core at same exposure on Sharpe OR worst dip): Sharpe "
                       f"{cs['sharpe']:.2f} vs {ms['sharpe']:.2f} -> {'yes' if sharpe_ok else 'no'}; worst dip "
                       f"-{cdip:.1f}% ({ctop:%Y-%m-%d} to {clow:%Y-%m-%d}) vs -{mdip:.1f}% "
                       f"({mtop:%Y-%m-%d} to {mlow:%Y-%m-%d}) -> {'yes' if dip_ok else 'no'}. 65/35 {verdict}; "
                       f"with margins of +0.10 Sharpe or 3 points: {'yes' if margin_ok else 'no'}.")
            if sat_first is not None and clow < sat_first and mlow < sat_first:
                out.append(f"  Both worst dips bottomed before the satellite's first trade ({sat_first:%Y-%m-%d}), when it "
                           f"was only cash: this dip comparison does not measure the satellite.")
        if measures_satellite and "Satellite only" in seen and "Core only" in seen:
            core_c, sat_c = res.curves["Core only"].dropna(), res.curves["Satellite only"].dropna()
            if start is not None:
                core_c, sat_c = core_c[core_c.index >= start], sat_c[sat_c.index >= start]
            rho, _ = _corr(core_c, sat_c)
            s_sat, s_core = seen["Satellite only"][0]["sharpe"], seen["Core only"][0]["sharpe"]
            if rho is not None:
                need = rho * s_core
                out.append(f"  Break-even (all in this window): the satellite lifts the account's Sharpe only if its "
                           f"Sharpe beats correlation x core Sharpe: {s_sat:.2f} vs {rho:.2f} x {s_core:.2f} = "
                           f"{need:.2f} -> {'yes' if s_sat > need else 'no'}.")
        return out

    any_curve = next(iter(res.curves.values())).dropna()
    lines = [f"Whole-account backtest: {res.capital:,.0f} {quote}, {res.fraction:.0%} core / "
             f"{1 - res.fraction:.0%} satellite, fees and slippage included"]
    if res.assumptions:
        lines.append(f"Satellite {res.assumptions[0].lower()}{res.assumptions[1:]} Core: market orders "
                     f"(taker fee + slippage). Satellite marked to market daily.")
    u = res.universe or {}
    saved_sel, now_sel = u.get("selection_saved"), u.get("selection_now")
    if saved_sel and now_sel and sorted(saved_sel) != sorted(now_sel):
        lines.append(f"NOTE: the strategy selection differs from the saved run's (then: {', '.join(saved_sel)}; now: "
                     f"{', '.join(now_sel)}). The selection was made on this same data, so any change in the satellite's "
                     f"result from adding or dropping a strategy is in-sample - not new evidence for the satellite.")
    lines += table(f"\nFull history ({any_curve.index[0]:%Y-%m-%d} to {any_curve.index[-1]:%Y-%m-%d}):", None)
    if res.since is not None and res.since > any_curve.index[0]:
        lines += table(f"\nSince {res.since:%Y-%m-%d} (each rebased to {res.capital:,.0f}):", res.since)
    if sat_first is not None and sat_first > any_curve.index[0]:
        lines += table(f"\nFrom the satellite's first trade ({sat_first:%Y-%m-%d}) - THE WINDOW THAT MEASURES THE "
                       f"SATELLITE (before it the satellite is only cash; benchmark re-matched to this window):",
                       sat_first, measures_satellite=True)
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
