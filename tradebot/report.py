"""Track record and the go-live readiness checklist."""
from __future__ import annotations

from dataclasses import dataclass

from .backtest.metrics import max_drawdown_pct, trade_metrics
from .config import BotConfig
from .db import Database


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


def readiness(db: Database, cfg: BotConfig, now_ms: int, mode: str = "paper",
              min_trades: int = 30, min_days: float = 14, min_pf: float = 1.2) -> list[Check]:
    closed = db.closed_positions(mode)
    m = trade_metrics([p.r_multiple or 0 for p in closed])
    first = min((p.opened_at for p in closed), default=now_ms)
    days = (now_ms - first) / 86_400_000
    curve = db.equity_curve(mode)
    dd = max_drawdown_pct(curve) if len(curve) else 0.0
    return [
        Check("enough trades", m["trades"] >= min_trades, f"{m['trades']} closed {mode} trades (need {min_trades})"),
        Check("long enough", days >= min_days, f"{days:.1f} days of {mode} trading (need {min_days})"),
        Check("positive expectancy", m["expectancy_r"] > 0, f"{m['expectancy_r']:+.3f}R per trade"),
        Check("profit factor", m["profit_factor"] >= min_pf, f"{m['profit_factor']:.2f} (need {min_pf})"),
        Check("drawdown within limit", dd < cfg.risk.max_drawdown_pct,
              f"max drawdown {dd:.1f}% (limit {cfg.risk.max_drawdown_pct}%)"),
    ]


def format_readiness(checks: list[Check]) -> str:
    lines = [f"  [{'PASS' if c.passed else 'FAIL'}] {c.name}: {c.detail}" for c in checks]
    verdict = "READY for live trading (start small!)" if all(c.passed for c in checks) \
        else "NOT ready for live trading - keep paper trading"
    return "\n".join(lines + ["", f"  => {verdict}"])


def sleeve_summary(db: Database, cfg: BotConfig, mode: str = "paper") -> str | None:
    """P&L by sleeve from the latest equity snapshot (None before the bot has recorded one)."""
    snaps = db.snapshots(mode)
    if snaps.empty:
        return None
    last = snaps.iloc[-1]
    start = float(db.kv_get("paper_starting_balance", cfg.paper.starting_balance) if mode == "paper"
                  else snaps["total"].iloc[0])
    core_in = float(db.kv_get(f"{mode}:core:contributed", 0.0) or 0.0)
    q = cfg.exchange.quote
    lines = [f"Sleeves ({mode}, as of {last.name:%Y-%m-%d %H:%M} UTC):",
             f"  Total      {last['total']:>12,.2f} {q}   P&L {last['total'] - start:+,.2f} "
             f"({(last['total'] / start - 1) * 100:+.1f}%)"]
    if core_in or last["core"]:
        sat_in = start - core_in
        lines.append(f"  Core       {last['core']:>12,.2f} {q}   P&L {last['core'] - core_in:+,.2f}")
        lines.append(f"  Satellite  {last['satellite']:>12,.2f} {q}   P&L {last['satellite'] - sat_in:+,.2f}")
    first_btc = snaps["btc_price"].dropna()
    if len(first_btc) > 1:
        btc = (first_btc.iloc[-1] / first_btc.iloc[0] - 1) * 100
        lines.append(f"  Holding BTC over the same period: {btc:+.1f}%")
    return "\n".join(lines)
