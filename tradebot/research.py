"""Research helpers: evaluate an optional rule on real data before switching it on."""
from __future__ import annotations

from dataclasses import dataclass

from .backtest.metrics import trade_metrics
from .backtest.selection import Selection, run_combo
from .config import BotConfig


@dataclass
class BreakerRow:
    combo: str
    ratio: float | None  # None = breaker off
    is_trades: int
    is_exp: float
    oos_trades: int
    oos_exp: float


def breaker_study(selection: Selection, datasets_by_tf: dict, cfg: BotConfig, context,
                  ratios=(2.0, 2.5, 3.0)) -> list[BreakerRow]:
    """Backtest each selected combo with the volatility breaker off and at each ratio."""
    rows = []
    for c in selection.selected:
        if c.timeframe not in datasets_by_tf:
            continue
        for ratio in (None, *ratios):
            is_t, oos_t, _ = run_combo(datasets_by_tf[c.timeframe], c.strategy, c.params, c.timeframe,
                                       cfg, context, vol_breaker=ratio)
            mi = trade_metrics([t.r_multiple for t in is_t])
            mo = trade_metrics([t.r_multiple for t in oos_t])
            rows.append(BreakerRow(c.key, ratio, mi["trades"], mi["expectancy_r"], mo["trades"], mo["expectancy_r"]))
    return rows


def format_breaker_study(rows: list[BreakerRow]) -> str:
    out = [f"{'combo':<16}{'breaker':>9}{'IS trades':>11}{'IS exp':>9}{'OOS trades':>12}{'OOS exp':>9}  verdict"]
    base = {}
    helps_everywhere = {}
    for r in rows:
        if r.ratio is None:
            base[r.combo] = r
            verdict = "(baseline)"
        else:
            b = base[r.combo]
            better = r.is_exp > b.is_exp and r.oos_exp > b.oos_exp
            helps_everywhere.setdefault(r.ratio, True)
            helps_everywhere[r.ratio] &= better
            verdict = "helps in both periods" if better else "does not help"
        label = "off" if r.ratio is None else f"{r.ratio:g}"
        out.append(f"{r.combo:<16}{label:>9}{r.is_trades:>11}{r.is_exp:>+9.3f}{r.oos_trades:>12}{r.oos_exp:>+9.3f}  {verdict}")
    good = [ratio for ratio, ok in helps_everywhere.items() if ok]
    out.append("")
    if good:
        out.append(f"Recommendation: the breaker improved every selected strategy in and out of sample at ratio(s) "
                   f"{', '.join(f'{g:g}' for g in good)}. To enable: guards.vol_breaker: true, "
                   f"guards.vol_breaker_ratio: {max(good):g}")
    else:
        out.append("Recommendation: keep the breaker OFF - it did not improve every selected strategy in both periods.")
    return "\n".join(out)
