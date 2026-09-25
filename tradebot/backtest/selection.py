"""Pick which (strategy, timeframe) combinations the bot is allowed to trade.

Each combination is backtested on every symbol. The first part of each history
is the in-sample period, the rest is out-of-sample. A combination is selected
only if it is profitable (after fees and slippage) in BOTH periods, trades often
enough to be meaningful and works on at least half of the symbols - i.e. it is
not a fluke of one coin or one period.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from ..config import BotConfig
from ..strategies import make_strategy
from .engine import Costs, Trade, backtest_populated, portfolio_simulation
from .metrics import summarize, trade_metrics


@dataclass
class ComboResult:
    strategy: str
    timeframe: str
    params: dict
    in_sample: dict
    out_of_sample: dict
    symbols_tested: int
    symbol_win_fraction: float
    selected: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.strategy}@{self.timeframe}"


@dataclass
class Selection:
    created_at: float
    combos: list[ComboResult]

    @property
    def selected(self) -> list[ComboResult]:
        return [c for c in self.combos if c.selected]

    def timeframes(self) -> list[str]:
        return sorted({c.timeframe for c in self.selected}, key=_tf_sort_key)

    def save(self, path: Path) -> None:
        payload = {"created_at": self.created_at, "combos": [asdict(c) for c in self.combos]}
        path.write_text(json.dumps(payload, indent=2, default=_json_default))

    @classmethod
    def load(cls, path: Path) -> "Selection | None":
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        return cls(created_at=d["created_at"], combos=[ComboResult(**c) for c in d["combos"]])


def _tf_sort_key(tf: str) -> int:
    from ..timeframes import tf_seconds

    return tf_seconds(tf)


def _json_default(o):
    if isinstance(o, float) and math.isinf(o):
        return 1e9
    return str(o)


def run_combo(
    datasets: dict[str, pd.DataFrame],
    strategy_name: str,
    params: dict,
    timeframe: str,
    cfg: BotConfig,
) -> tuple[list[Trade], list[Trade], dict[str, float]]:
    """Backtest one combination on all symbols -> (in-sample, out-of-sample, per-symbol exp)."""
    strategy = make_strategy(strategy_name, params)
    costs = Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate)
    frac = cfg.selection.in_sample_fraction
    is_trades, oos_trades, per_symbol = [], [], {}
    for symbol, df in datasets.items():
        if len(df) < strategy.warmup + 100:
            continue
        pop = strategy.populate(df)
        split = strategy.warmup + int((len(df) - strategy.warmup) * frac)
        split_time = pop.index[min(split, len(pop) - 1)]
        trades = backtest_populated(
            pop, strategy, costs, symbol=symbol, timeframe=timeframe,
            allow_short=cfg.allow_short, breakeven_at_r=cfg.risk.breakeven_at_r,
            min_reward_risk=cfg.risk.min_reward_risk,
        )
        admitted = []
        for t in trades:
            if t.signal_idx >= split:
                oos_trades.append(t)
            elif t.exit_time < split_time:
                is_trades.append(t)
            else:
                continue  # straddles the split - purged from both periods and from the score
            admitted.append(t)
        if len(admitted) >= 3:
            per_symbol[symbol] = sum(t.r_multiple for t in admitted) / len(admitted)
    return is_trades, oos_trades, per_symbol


def evaluate_combo(datasets, strategy_name, params, timeframe, cfg: BotConfig) -> ComboResult:
    is_trades, oos_trades, per_symbol = run_combo(datasets, strategy_name, params, timeframe, cfg)
    r = cfg.risk
    sims = {}
    for label, trades in (("is", is_trades), ("oos", oos_trades)):
        curve, taken = portfolio_simulation(
            trades, risk_per_trade_pct=r.risk_per_trade_pct,
            max_position_pct=r.max_position_pct, max_open_positions=r.max_open_positions,
        )
        m = trade_metrics([t.r_multiple for t in trades], [t.bars_held for t in trades])
        pm = summarize(taken, curve)
        m["portfolio_return_pct"] = pm.get("total_return_pct", 0.0)
        m["portfolio_max_dd_pct"] = pm.get("max_drawdown_pct", 0.0)
        sims[label] = m
    win_frac = (
        sum(1 for v in per_symbol.values() if v > 0) / len(per_symbol) if per_symbol else 0.0
    )

    s = cfg.selection
    reasons = []
    ins, oos = sims["is"], sims["oos"]
    if ins["trades"] < s.min_trades_in_sample:
        reasons.append(f"too few in-sample trades ({ins['trades']} < {s.min_trades_in_sample})")
    if oos["trades"] < s.min_trades_out_of_sample:
        reasons.append(f"too few out-of-sample trades ({oos['trades']} < {s.min_trades_out_of_sample})")
    for label, m in (("in-sample", ins), ("out-of-sample", oos)):
        if m["trades"] and m["expectancy_r"] < s.min_expectancy_r:
            reasons.append(f"{label} expectancy {m['expectancy_r']:+.3f}R < {s.min_expectancy_r}")
        if m["trades"] and m["profit_factor"] < s.min_profit_factor:
            reasons.append(f"{label} profit factor {m['profit_factor']:.2f} < {s.min_profit_factor}")
    if win_frac < s.min_symbol_win_fraction:
        reasons.append(f"profitable on only {win_frac:.0%} of symbols")
    return ComboResult(
        strategy=strategy_name,
        timeframe=timeframe,
        params=params,
        in_sample=ins,
        out_of_sample=oos,
        symbols_tested=len(per_symbol),
        symbol_win_fraction=win_frac,
        selected=not reasons,
        reasons=reasons,
    )


def run_selection(
    datasets_by_tf: dict[str, dict[str, pd.DataFrame]], cfg: BotConfig, log=print
) -> Selection:
    combos = []
    for tf, datasets in datasets_by_tf.items():
        for name, params in cfg.strategies.items():
            res = evaluate_combo(datasets, name, params, tf, cfg)
            status = "SELECTED" if res.selected else "rejected"
            log(
                f"  {res.key:<16} {status:<8} IS {res.in_sample['trades']:>4} trades "
                f"{res.in_sample['expectancy_r']:+.3f}R | OOS {res.out_of_sample['trades']:>4} trades "
                f"{res.out_of_sample['expectancy_r']:+.3f}R"
                + ("" if res.selected else f"  ({res.reasons[0]})")
            )
            combos.append(res)
    return Selection(created_at=time.time(), combos=combos)
