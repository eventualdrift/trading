import numpy as np
import pandas as pd
import pytest

from tradebot.config import BotConfig
from tradebot.portfolio import combine_sleeves, curve_stats, format_portfolio_backtest, portfolio_backtest, yearly_returns


def daily(values, start="2022-01-01"):
    return pd.Series(np.asarray(values, dtype=float), index=pd.date_range(start, periods=len(values), freq="D", tz="UTC"))


def test_combine_without_resets_is_the_weighted_sum():
    core = daily(np.linspace(1.0, 2.0, 100))
    sat = daily(np.linspace(1.0, 0.5, 100))
    out = combine_sleeves(core, sat, 0.6, 0, 1000.0)
    assert out.iloc[-1] == pytest.approx(600 * 2.0 + 400 * 0.5)


def test_resets_rebalance_to_the_split():
    core = daily(np.cumprod(np.full(90, 1.01)))  # +1%/day
    sat = daily(np.ones(90))  # flat
    no_reset = combine_sleeves(core, sat, 0.5, 0, 1000.0).iloc[-1]
    monthly = combine_sleeves(core, sat, 0.5, 30, 1000.0).iloc[-1]
    assert monthly < no_reset  # resets move winnings from the rising sleeve into the flat one


def test_stats_and_yearly_returns():
    eq = daily([100, 110, 99, 120] + [120] * 361)
    s = curve_stats(eq)
    assert s["total_pct"] == pytest.approx(20.0) and s["max_dd_pct"] == pytest.approx(10.0)
    y = yearly_returns(daily(np.linspace(100, 200, 730)))
    assert list(y.index) == [2022, 2023] and (y > 0).all()


def test_portfolio_backtest_end_to_end():
    from tradebot.data.synthetic import generate_ohlcv
    from tradebot.timeframes import resample_ohlcv

    closes = {s: resample_ohlcv(generate_ohlcv(24 * 4 * 800, "15m", seed=k), "1d")["close"]
              for k, s in enumerate(["BTC/USDT", "ETH/USDT"])}
    res = portfolio_backtest(closes, [], BotConfig(), capital=1000, fraction=0.65, since="2025-01-01")
    assert {"Combined (65% core)", "Core only", "Satellite only", "Hold BTC", "Hold BTC+ETH"} <= set(res.curves)
    assert res.curves["Satellite only"].iloc[-1] == pytest.approx(1000.0)  # no trades: cash
    text = format_portfolio_backtest(res)
    assert "Full history" in text and "Since 2025-01-01" in text and "Year by year" in text
