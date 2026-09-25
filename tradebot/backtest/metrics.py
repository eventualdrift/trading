from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pandas as pd


def max_drawdown_pct(equity: pd.Series, start_equity: float | None = None) -> float:
    if equity.empty:
        return 0.0
    values = equity.to_numpy(dtype=float)
    if start_equity is not None:
        values = np.concatenate([[start_equity], values])
    peak = np.maximum.accumulate(values)
    dd = (values - peak) / peak
    return float(-dd.min() * 100.0) + 0.0  # (+0.0 turns -0.0 into 0.0)


def trade_metrics(r_multiples: Iterable[float], bars_held: Iterable[int] | None = None) -> dict:
    r = np.asarray(list(r_multiples), dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0, "win_rate": 0.0, "expectancy_r": 0.0, "profit_factor": 0.0,
                "avg_win_r": 0.0, "avg_loss_r": 0.0, "total_r": 0.0, "sqn": 0.0}
    wins, losses = r[r > 0], r[r <= 0]
    gross_loss = -losses.sum()
    pf = float(wins.sum() / gross_loss) if gross_loss > 0 else math.inf
    sd = r.std(ddof=1) if n > 1 else 0.0
    out = {
        "trades": n,
        "win_rate": float(len(wins) / n),
        "expectancy_r": float(r.mean()),
        "profit_factor": pf,
        "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
        "total_r": float(r.sum()),
        "sqn": float(math.sqrt(n) * r.mean() / sd) if sd > 0 else 0.0,
    }
    if bars_held is not None:
        b = list(bars_held)
        out["avg_bars_held"] = float(np.mean(b)) if b else 0.0
    return out


def summarize(trades, equity: pd.Series | None = None, start_equity: float = 1000.0) -> dict:
    m = trade_metrics([t.r_multiple for t in trades], [t.bars_held for t in trades])
    if equity is not None and not equity.empty:
        m["total_return_pct"] = float((equity.iloc[-1] / start_equity - 1) * 100)
        m["max_drawdown_pct"] = max_drawdown_pct(equity, start_equity)
    return m


def format_metrics(m: dict) -> str:
    pf = m.get("profit_factor", 0)
    pf_s = "inf" if pf == math.inf else f"{pf:.2f}"
    s = (
        f"trades={m['trades']} win={m['win_rate']:.0%} exp={m['expectancy_r']:+.3f}R "
        f"PF={pf_s} totalR={m['total_r']:+.1f} SQN={m['sqn']:.2f}"
    )
    if "total_return_pct" in m:
        s += f" return={m['total_return_pct']:+.1f}% maxDD={m['max_drawdown_pct']:.1f}%"
    return s
