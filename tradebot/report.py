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
