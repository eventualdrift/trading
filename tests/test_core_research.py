import numpy as np
import pandas as pd
import pytest

from tradebot.config import BotConfig
from tradebot.core_research import (common_start, format_core_robustness, reset_phase_study, sma_scale_study,
                                    windows)
from tradebot.portfolio import combine_sleeves_detail, format_portfolio_backtest, portfolio_backtest

from .test_portfolio import trade


def market(days=1200, seed=5):
    idx = pd.date_range("2019-01-01", periods=days, freq="D", tz="UTC")
    rng = np.random.default_rng(seed)
    return {"BTC/USDT": pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.03, days)), index=idx),
            "ETH/USDT": pd.Series(50 * np.cumprod(1 + rng.normal(0.001, 0.04, days)), index=idx)}


def test_reset_anchor_moves_the_schedule():
    idx = pd.date_range("2022-01-01", periods=90, freq="D", tz="UTC")
    core = pd.Series(np.cumprod(np.full(90, 1.01)), index=idx)
    flat = pd.Series(1.0, index=idx)
    base = combine_sleeves_detail(core, flat, 0.5, 30, 1000.0)
    same = combine_sleeves_detail(core, flat, 0.5, 30, 1000.0, anchor=idx[0])
    assert np.allclose(base["total"], same["total"])
    shifted = combine_sleeves_detail(core, flat, 0.5, 30, 1000.0, anchor=idx[0] - pd.Timedelta(days=10))
    # first reset on day 20 instead of day 30: the split is restored 10 days earlier
    assert shifted["core"].iloc[20] / shifted["total"].iloc[20] == pytest.approx(0.5)
    assert base["core"].iloc[20] / base["total"].iloc[20] > 0.5


def test_phase_zero_is_the_backtest_and_all_phases_are_reported():
    closes = market()
    trades = [trade("SOL/USDT", d, d + 8, 10.0, 10.6, start="2019-01-01") for d in range(700, 1150, 6)]
    cfg = BotConfig()
    res = portfolio_backtest(closes, trades, cfg, capital=1000, fraction=0.65, since="2021-01-01")
    study = reset_phase_study(res)
    assert study["phases"] == 30
    full = study["rows"][windows(res)[0][0]]["combined"]
    assert len(full) == 30
    base_curve = res.curves["Combined (65% core)"]
    assert full[0]["cagr"] == pytest.approx(
        ((base_curve.iloc[-1] / base_curve.iloc[0]) ** (365.25 / (base_curve.index[-1] - base_curve.index[0]).days) - 1) * 100,
        rel=1e-6)
    scales = sma_scale_study(closes, res, cfg)
    assert [v["days"] for v in scales["variants"]] == [[40, 80, 120, 160], [50, 100, 150, 200], [60, 120, 180, 240]]
    assert scales["start"] == common_start(closes, cfg.core.sma_days)
    assert scales["start"] >= closes["BTC/USDT"].index[239]  # the 240-day average needs 240 days
    text = format_core_robustness(res, study, scales, cfg)
    assert "REPORTING ONLY" in text and "30 possible phases" in text and "<- live" in text
    assert "1.2x (60/120/180/240)" in text and "spread (max - min)" in text


def test_report_flags_a_changed_strategy_selection():
    closes = market(600)
    res = portfolio_backtest(closes, [], BotConfig(), capital=1000, fraction=0.65, since=None,
                             universe={"symbols": ["BTC/USDT"], "selection_saved": ["momentum@4h", "breakout@1d"],
                                       "selection_now": ["breakout@1d"]})
    text = format_portfolio_backtest(res)
    assert "strategy selection differs from the saved run's" in text and "not new evidence" in text
