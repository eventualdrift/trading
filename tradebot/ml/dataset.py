"""Turn price history into a labelled training set.

Every entry signal a strategy would ever have produced is simulated on its own
(with the same fees/slippage/stop rules as the backtester). The label is whether
the trade made money. The model later learns which setups tend to win.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest.engine import reward_risk, simulate_trade
from ..config import BotConfig
from ..strategies import make_strategy
from .features import FEATURE_COLUMNS, candidate_features, market_features

# ("side" is already a numeric feature: +1 long, -1 short)
META_COLUMNS = ["symbol", "timeframe", "strategy", "signal_time", "exit_time", "r_multiple", "label"]


def candidates_for(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    strategy_name: str,
    params: dict,
    cfg: BotConfig,
    mkt: pd.DataFrame | None = None,
    context=None,
) -> pd.DataFrame:
    strategy = make_strategy(strategy_name, params)
    if len(df) < strategy.warmup + 50:
        return pd.DataFrame(columns=FEATURE_COLUMNS + META_COLUMNS)
    pop = strategy.populate(df, context, timeframe)
    mkt = market_features(df, context, timeframe) if mkt is None else mkt
    costs = cfg.costs_model()
    o, h, l, c = (pop[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    rows = []
    sides = [("long", "enter_long", "exit_long", "long_sl", "long_tp")]
    if cfg.allow_short:
        sides.append(("short", "enter_short", "exit_short", "short_sl", "short_tp"))
    for side, enter, exit_col, sl_col, tp_col in sides:
        flags = pop[enter].to_numpy()
        exits = pop[exit_col].to_numpy()
        sls, tps = pop[sl_col].to_numpy(dtype=float), pop[tp_col].to_numpy(dtype=float)
        trails = pop["trail_dist"].to_numpy(dtype=float)
        for i in np.flatnonzero(flags):
            if i < strategy.warmup:
                continue
            if reward_risk(side, c[i], sls[i], tps[i]) < cfg.risk.min_reward_risk:
                continue
            out = simulate_trade(
                o, h, l, c, exits, i, side, sls[i], tps[i], strategy.max_hold_bars,
                costs, cfg.risk.breakeven_at_r, trails[i],
            )
            if out is None or not out.complete:
                continue
            rows.append((i, side, sls[i], tps[i], out.exit_idx, out.r_multiple))
    if not rows:
        return pd.DataFrame(columns=FEATURE_COLUMNS + META_COLUMNS)
    idx = np.array([r[0] for r in rows])
    sides_arr = np.array([r[1] for r in rows])
    X = candidate_features(
        mkt.iloc[idx], sides_arr, np.full(len(rows), strategy_name), timeframe,
        c[idx], np.array([r[2] for r in rows]), np.array([r[3] for r in rows]),
    )
    X = X.reset_index(drop=True)
    X["symbol"] = symbol
    X["timeframe"] = timeframe
    X["strategy"] = strategy_name
    X["signal_time"] = pop.index[idx]
    X["exit_time"] = pop.index[[r[4] for r in rows]]
    X["r_multiple"] = [r[5] for r in rows]
    X["label"] = (X["r_multiple"] > 0).astype(int)
    return X


def build_candidates(
    datasets_by_tf: dict[str, dict[str, pd.DataFrame]], cfg: BotConfig, context=None,
    params_for: dict[tuple[str, str], dict] | None = None,
) -> pd.DataFrame:
    """``params_for[(strategy, timeframe)]`` overrides the config params (e.g. with the
    BTC filter that selection chose), so the model learns from the signals it will see."""
    frames = []
    for tf, datasets in datasets_by_tf.items():
        for symbol, df in datasets.items():
            if len(df) < 300:
                continue
            mkt = market_features(df, context, tf)
            for name, params in cfg.strategies.items():
                p = (params_for or {}).get((name, tf), params)
                frames.append(candidates_for(df, symbol, tf, name, p, cfg, mkt, context))
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=FEATURE_COLUMNS + META_COLUMNS)
    out = pd.concat(frames, ignore_index=True)
    return out.sort_values("signal_time", kind="stable").reset_index(drop=True)
