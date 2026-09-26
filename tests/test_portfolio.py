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


def trade(symbol, entry_day, exit_day, entry, exit_, stop_pct=0.1, start="2022-01-01"):
    from tradebot.backtest.engine import Trade

    t0 = pd.Timestamp(start, tz="UTC")
    ret = (exit_ * (1 - 0.001) - entry * (1 + 0.001)) / entry
    return Trade(symbol=symbol, timeframe="1d", strategy="momentum", side="long", signal_idx=entry_day - 1,
                 signal_time=t0 + pd.Timedelta(days=entry_day - 1), entry_time=t0 + pd.Timedelta(days=entry_day),
                 exit_time=t0 + pd.Timedelta(days=exit_day), entry_price=entry, exit_price=exit_,
                 stop_loss=entry * (1 - stop_pct), take_profit=entry * 2, reason="exit_signal",
                 bars_held=exit_day - entry_day, r_multiple=ret / stop_pct, return_pct=ret, stop_pct=stop_pct)


def test_satellite_is_marked_to_market_and_matches_the_realised_replay():
    from tradebot.backtest.engine import portfolio_simulation
    from tradebot.portfolio import simulate_satellite

    cfg = BotConfig()
    idx = pd.date_range("2022-01-01", periods=60, freq="D", tz="UTC")
    # AAA falls 40% in the middle of the trade, then exits at +10%
    aaa = pd.Series(np.r_[np.full(10, 100.0), np.linspace(100, 60, 10), np.linspace(60, 110, 10), np.full(30, 110.0)],
                    index=idx)
    trades = [trade("AAA/USDT", 5, 30, 100.0, 110.0),
              trade("BBB/USDT", 6, 12, 50.0, 55.0), trade("CCC/USDT", 7, 13, 20.0, 19.0),
              trade("DDD/USDT", 8, 14, 10.0, 11.0),  # 4th at once: skipped (max 3 open)
              trade("AAA/USDT", 20, 25, 80.0, 90.0)]  # AAA already held: skipped
    run = simulate_satellite(trades, cfg, idx, {"AAA/USDT": aaa})
    _, taken = portfolio_simulation(trades, risk_per_trade_pct=1.0, max_position_pct=30.0, max_open_positions=3,
                                    start_equity=1.0)
    assert {t.symbol for t in run.taken} == {t.symbol for t in taken} and len(run.taken) == 3
    assert {k: len(v) for k, v in run.skipped.items()} == {"max open positions (3)": 1, "already holding that coin": 1}
    assert run.equity.iloc[-1] == pytest.approx(run.realized_end)
    # while AAA was 40% down the marked curve shows it; a realised-only curve would not
    size = run.sizes[0]  # 1% risk / 10% stop = 10% of equity
    assert size == pytest.approx(0.1)
    assert run.equity.min() < 1.0 - 0.1 * 0.35
    assert run.exposure.iloc[5] == pytest.approx(0.1, rel=0.02) and run.exposure.iloc[-1] == 0
    assert run.open_count.max() == 3


def test_portfolio_backtest_reports_the_satellite_measurement():
    cfg = BotConfig()
    idx = pd.date_range("2021-01-01", periods=900, freq="D", tz="UTC")
    rng = np.random.default_rng(1)
    btc = pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.03, 900)), index=idx)
    eth = pd.Series(50 * np.cumprod(1 + rng.normal(0.001, 0.04, 900)), index=idx)
    trades = [trade("SOL/USDT", d, d + 8, 10.0, 10.5 if d % 3 else 9.2, start="2021-01-01") for d in range(400, 880, 5)]
    sol = pd.Series(10.0, index=idx)
    from tradebot.portfolio import combo_split_stats

    res = portfolio_backtest({"BTC/USDT": btc, "ETH/USDT": eth}, trades, cfg, capital=1000, fraction=0.65,
                             since="2022-06-01", sat_closes={"SOL/USDT": sol},
                             combos=[combo_split_stats("momentum@1d", trades[:60], trades[60:])],
                             universe={"source": "today's top 30", "symbols": ["SOL/USDT"],
                                       "first": {"SOL/USDT": idx[0]}})
    text = format_portfolio_backtest(res)
    for needle in ("Candidate trades: 96", "taken", "Position size at entry", "Exposure", "IS    60 trades",
                   "OOS   36 trades", "Correlation, core vs satellite", "NOT the coins listed at the time",
                   "Core at same exposure", "Satellite entries: market", "marked to market"):
        assert needle in text, needle
