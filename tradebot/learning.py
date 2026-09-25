"""The self-learning cycle: refresh data -> re-select strategies -> retrain ML filter."""
from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from .backtest.selection import Selection, run_selection
from .config import BotConfig
from .ml.dataset import build_candidates
from .ml.model import ModelReport, SignalModel, save_report, train_model

log = logging.getLogger(__name__)

SELECTION_FILE = "selection.json"
MODEL_FILE = "model.joblib"
MODEL_REPORT_FILE = "model_report.json"


@dataclass
class LearningResult:
    selection: Selection
    model: SignalModel | None  # newly promoted model, or None (keep the current one)
    report: ModelReport | None
    summary: str


def load_brain(cfg: BotConfig) -> tuple[Selection | None, SignalModel | None]:
    return (
        Selection.load(cfg.state_path / SELECTION_FILE),
        SignalModel.load(cfg.state_path / MODEL_FILE) if cfg.ml.enabled else None,
    )


def load_datasets(market, cfg: BotConfig, symbols: list[str], store=None, now_ms: int | None = None,
                  log_fn=print) -> dict[str, dict[str, pd.DataFrame]]:
    now = now_ms or market.now_ms()
    out: dict[str, dict[str, pd.DataFrame]] = {}
    for tf in cfg.timeframes:
        days = cfg.data.history_days.get(tf, 365)
        out[tf] = {}
        for sym in symbols:
            try:
                if store is not None:
                    df = store.update(market, sym, tf, days, now)
                else:
                    df = market.history(sym, tf, now - days * 86_400_000, now)
            except Exception as exc:
                log_fn(f"  ! {sym} {tf}: {exc}")
                continue
            if len(df):
                out[tf][sym] = df
        log_fn(f"  {tf}: {len(out[tf])} symbols, {sum(len(d) for d in out[tf].values()):,} candles")
    return out


def learning_cycle(cfg: BotConfig, market, *, store=None, db=None, current_model: SignalModel | None = None,
                   now_ms: int | None = None, log_fn=print) -> LearningResult:
    u = cfg.universe
    symbols = market.top_symbols(cfg.exchange.quote, u.top_n, u.min_quote_volume, u.whitelist, u.blacklist)
    log_fn(f"Universe: {len(symbols)} symbols: {', '.join(symbols)}")
    log_fn("Loading history...")
    datasets = load_datasets(market, cfg, symbols, store, now_ms, log_fn)

    log_fn("Selecting strategies (in-sample vs out-of-sample backtests)...")
    selection = run_selection(datasets, cfg, log=log_fn)
    selection.save(cfg.state_path / SELECTION_FILE)

    model, report = None, None
    if cfg.ml.enabled:
        log_fn("Training ML trade filter (walk-forward)...")
        cands = build_candidates(datasets, cfg)
        if db is not None:
            own = db.trade_samples()
            if not own.empty:
                cands = pd.concat([cands, own], ignore_index=True)
                log_fn(f"  + {len(own)} of the bot's own closed trades")
        log_fn(f"  {len(cands):,} historical signals")
        focus = {(c.strategy, c.timeframe) for c in selection.selected}
        model, report = train_model(cands, cfg.ml, current_model, focus=focus or None)
        save_report(report, cfg.state_path / MODEL_REPORT_FILE)
        if model is not None:
            model.save(cfg.state_path / MODEL_FILE)

    sel = selection.selected
    lines = [f"Strategies selected: {len(sel)} of {len(selection.combos)} tested"]
    for c in sel:
        o = c.out_of_sample
        lines.append(f"  • {c.key}: out-of-sample {o['trades']} trades, {o['expectancy_r']:+.2f}R/trade, "
                     f"win {o['win_rate']:.0%}, PF {min(o['profit_factor'], 99):.2f}")
    if not sel:
        lines.append("  No strategy passed validation - the bot will NOT trade until one does.")
    if report is not None:
        lines.append(report.summary())
    return LearningResult(selection, model, report, "\n".join(lines))
