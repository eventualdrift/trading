"""What could the account become? A Monte Carlo projection from real backtest trades.

Takes the out-of-sample trades of the selected strategies (never used to pick them),
replays them as one account with the live risk limits, then simulates thousands of
possible futures by drawing trades at random at the historical rate. The result is
a range - bad, typical, good - not a promise: markets change, and the past sample
may be lucky or unlucky.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtest.engine import Trade, portfolio_simulation
from .backtest.metrics import max_drawdown_pct
from .backtest.selection import Selection, run_combo
from .config import BotConfig

MONTH_DAYS = 30.44


@dataclass
class Projection:
    capital: float
    runs: int
    trades: int
    span_days: float
    trades_per_month: float
    avg_trade_pct: float  # mean equity change per trade, %
    oos_return_pct: float
    oos_max_dd_pct: float
    rows: dict[int, dict[str, float]] = field(default_factory=dict)  # month -> stats
    benchmark_pct: float | None = None  # buy & hold BTC over the same period
    benchmark_dd_pct: float | None = None
    benchmark_symbol: str | None = None


def out_of_sample_trades(selection: Selection, datasets_by_tf: dict, cfg: BotConfig) -> list[Trade]:
    trades: list[Trade] = []
    for c in selection.selected:
        if c.timeframe in datasets_by_tf:
            _, oos, _ = run_combo(datasets_by_tf[c.timeframe], c.strategy, c.params, c.timeframe, cfg)
            trades += oos
    return trades


def equity_returns(trades: list[Trade], cfg: BotConfig) -> np.ndarray:
    """Each trade's effect on the account (fraction), sized exactly like the live bot."""
    r = cfg.risk
    return np.array([
        min(r.risk_per_trade_pct / 100.0 / max(t.stop_pct, 1e-9), r.max_position_pct / 100.0) * t.return_pct
        for t in trades
    ])


def project(
    trades: list[Trade],
    cfg: BotConfig,
    *,
    capital: float = 1000.0,
    months: tuple[int, ...] = (1, 2, 3, 6, 12),
    runs: int = 5000,
    seed: int = 0,
    benchmark: pd.DataFrame | None = None,
    benchmark_symbol: str | None = None,
) -> Projection:
    if not trades:
        raise ValueError("no out-of-sample trades to project from - run `tradebot learn` first")
    r = cfg.risk
    curve, taken = portfolio_simulation(trades, risk_per_trade_pct=r.risk_per_trade_pct,
                                        max_position_pct=r.max_position_pct,
                                        max_open_positions=r.max_open_positions, start_equity=capital)
    if not taken:
        raise ValueError("no trades survived the position limits")
    start = min(t.entry_time for t in taken)
    end = max(t.exit_time for t in taken)
    span_days = max((end - start).total_seconds() / 86400.0, 1.0)
    rate = len(taken) / (span_days / MONTH_DAYS)
    per_trade = equity_returns(taken, cfg)

    rng = np.random.default_rng(seed)
    equity = np.full(runs, float(capital))
    peak = equity.copy()
    worst_dd = np.zeros(runs)
    rows: dict[int, dict[str, float]] = {}
    for m in range(1, max(months) + 1):
        n = rng.poisson(rate, runs)
        k = int(n.max()) if n.size else 0
        if k:
            draws = rng.choice(per_trade, size=(runs, k))
            used = np.arange(k)[None, :] < n[:, None]
            steps = np.where(used, 1.0 + draws, 1.0)
            full = np.concatenate([equity[:, None], equity[:, None] * np.cumprod(steps, axis=1)], axis=1)
            running = np.maximum(np.maximum.accumulate(full, axis=1), peak[:, None])
            worst_dd = np.maximum(worst_dd, ((running - full) / running).max(axis=1))
            peak = running[:, -1]
            equity = full[:, -1]
        if m in months:
            q = np.percentile(equity, [5, 25, 50, 75, 95])
            rows[m] = {
                "p5": q[0], "p25": q[1], "median": q[2], "p75": q[3], "p95": q[4],
                "p_loss": float((equity < capital).mean()),
                "median_worst_dd_pct": float(np.median(worst_dd) * 100),
            }

    bench = bench_dd = None
    if benchmark is not None and len(benchmark):
        window = benchmark[(benchmark.index >= start) & (benchmark.index <= end)]["close"]
        if len(window) > 1:
            bench = float((window.iloc[-1] / window.iloc[0] - 1) * 100)
            bench_dd = max_drawdown_pct(window)
    return Projection(
        capital=capital, runs=runs, trades=len(taken), span_days=span_days, trades_per_month=rate,
        avg_trade_pct=float(per_trade.mean() * 100),
        oos_return_pct=float((curve.iloc[-1] / capital - 1) * 100) if len(curve) else 0.0,
        oos_max_dd_pct=max_drawdown_pct(curve, capital) if len(curve) else 0.0,
        rows=rows, benchmark_pct=bench, benchmark_dd_pct=bench_dd, benchmark_symbol=benchmark_symbol,
    )


def format_projection(p: Projection, quote: str = "USDT") -> str:
    lines = [
        f"Projection for {p.capital:,.0f} {quote} - {p.runs:,} simulated futures built from {p.trades} "
        f"out-of-sample trades ({p.span_days:.0f} days, ~{p.trades_per_month:.0f} trades/month, "
        f"{p.avg_trade_pct:+.2f}% of the account per trade on average)",
        "",
        f"{'':<10}{'bad (5%)':>11}{'weak (25%)':>12}{'typical':>11}{'good (75%)':>12}{'great (95%)':>13}"
        f"{'chance of loss':>16}",
    ]
    for m, s in p.rows.items():
        label = f"{m} month" + ("s" if m > 1 else "")
        lines.append(
            f"{label:<10}{s['p5']:>11,.0f}{s['p25']:>12,.0f}{s['median']:>11,.0f}{s['p75']:>12,.0f}"
            f"{s['p95']:>13,.0f}{s['p_loss']:>15.0%}"
        )
    last = p.rows[max(p.rows)]
    lines += [
        "",
        f"Typical worst dip along the way ({max(p.rows)}-month path): {last['median_worst_dd_pct']:.0f}% below the peak.",
        f"Over the tested period the bot made {p.oos_return_pct:+.1f}% (worst dip {p.oos_max_dd_pct:.1f}%).",
    ]
    if p.benchmark_pct is not None:
        verdict = ("more than" if p.oos_return_pct > p.benchmark_pct else "LESS than")
        lines.append(f"Simply holding {p.benchmark_symbol} made {p.benchmark_pct:+.1f}% (worst dip {p.benchmark_dd_pct:.1f}%) "
                     f"- the bot made {verdict} holding.")
    lines += [
        "",
        "Read this as a range, not a promise: it assumes the future looks like the tested past. "
        "Fees and slippage are included; the ML filter and confidence sizing are not.",
    ]
    return "\n".join(lines)
