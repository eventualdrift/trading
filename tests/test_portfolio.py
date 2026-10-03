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
    for needle in ("Candidate trades (every signal): 96", "taken", "Position size at entry", "Exposure", "IS    60 trades",
                   "OOS   36 trades", "Correlation, core vs satellite", "NOT the coins listed at the time",
                   "Core at same exposure", "Satellite entries: market", "marked to market"):
        assert needle in text, needle


def test_full_slots_are_filled_in_the_live_bots_order():
    """Same-day candidates compete for slots by reward:risk (the live scanner's rank), not by name."""
    from tradebot.backtest.engine import portfolio_simulation
    from tradebot.portfolio import simulate_satellite

    idx = pd.date_range("2022-01-01", periods=30, freq="D", tz="UTC")
    same_day = []
    for sym, rr in (("AAA/USDT", 1.5), ("BBB/USDT", 8.0), ("CCC/USDT", 2.0), ("DDD/USDT", 8.0)):
        t = trade(sym, 5, 10, 100.0, 105.0)
        t.signal_rr = rr
        same_day.append(t)
    cfg = BotConfig()
    run = simulate_satellite(same_day, cfg, idx)
    _, taken = portfolio_simulation(same_day, risk_per_trade_pct=1.0, max_position_pct=30.0,
                                    max_open_positions=3, start_equity=1.0)
    expected = {"BBB/USDT", "DDD/USDT", "CCC/USDT"}  # the two 8R setups, then 2R; AAA (1.5R) waits
    assert {t.symbol for t in run.taken} == expected == {t.symbol for t in taken}


def test_open_risk_budget_frees_room_once_open_trades_are_protected():
    from tradebot.portfolio import simulate_satellite

    idx = pd.date_range("2022-01-01", periods=60, freq="D", tz="UTC")
    t0 = pd.Timestamp("2022-01-01", tz="UTC")
    early = [trade(s, 1, 40, 100.0, 120.0) for s in ("AAA/USDT", "BBB/USDT", "CCC/USDT")]
    late = [trade(s, 10, 20, 100.0, 105.0) for s in ("DDD/USDT", "EEE/USDT")]
    cfg = BotConfig()
    # the 3-position rule: both late trades are skipped
    base = simulate_satellite(early + late, cfg, idx, open_risk_pct=None)
    assert len(base.taken) == 3 and len(base.skipped["max open positions (3)"]) == 2
    # a 3% budget with nothing protected: same result (3 x 1% already at stake)
    fresh = simulate_satellite(early + late, cfg, idx, open_risk_pct=3.0)
    assert len(fresh.taken) == 3 and len(fresh.skipped["open-risk budget (3%)"]) == 2
    # two early trades reach breakeven on day 5: their risk no longer counts, so both late ones fit
    for t in early[:2]:
        t.protected_time = t0 + pd.Timedelta(days=5)
    freed = simulate_satellite(early + late, cfg, idx, open_risk_pct=3.0)
    assert len(freed.taken) == 5 and not freed.skipped
    assert freed.open_count.max() == 5


def test_measurement_shows_which_strategies_got_the_slots():
    idx = pd.date_range("2021-01-01", periods=400, freq="D", tz="UTC")
    closes = {"BTC/USDT": pd.Series(np.linspace(100, 200, 400), index=idx),
              "ETH/USDT": pd.Series(np.linspace(50, 80, 400), index=idx)}
    trades = []
    for d in range(250, 390, 4):
        a = trade(f"A{d}/USDT", d, d + 30, 10.0, 10.5, start="2021-01-01")
        a.strategy, a.timeframe, a.signal_rr = "momentum", "4h", 8.0
        b = trade(f"B{d}/USDT", d, d + 3, 10.0, 11.0, start="2021-01-01")
        b.strategy, b.signal_rr = "breakout", 2.0
        trades += [a, b]
    res = portfolio_backtest(closes, trades, BotConfig(), capital=1000, fraction=0.65, since=None)
    text = format_portfolio_backtest(res)
    assert "By strategy" in text and "momentum@4h" in text and "breakout@1d" in text


def test_skips_are_attributed_to_same_close_ranking_or_slots_already_full():
    from tradebot.portfolio import simulate_satellite

    idx = pd.date_range("2022-01-01", periods=60, freq="D", tz="UTC")
    held = []
    for s in ("A1/USDT", "A2/USDT", "A3/USDT"):  # three 4h trades fill the slots on day 2
        t = trade(s, 2, 30, 100.0, 101.0)
        t.strategy, t.timeframe, t.signal_rr = "momentum", "4h", 8.0
        held.append(t)
    late = trade("L/USDT", 10, 15, 100.0, 105.0)  # daily breakout: slots already full
    late.strategy = "breakout"
    same = []  # day 40: four daily signals at one close compete for 3 free slots
    for s, rr in (("S1/USDT", 8.0), ("S2/USDT", 3.0), ("S3/USDT", 2.5), ("S4/USDT", 2.0)):
        t = trade(s, 40, 45, 100.0, 101.0)
        t.strategy = "breakout" if rr < 8 else "momentum"
        t.signal_rr = rr
        same.append(t)
    run = simulate_satellite(held + [late] + same, BotConfig(), idx, open_risk_pct=None)
    by_symbol = {d["trade"].symbol: d for d in run.skip_detail}
    assert set(by_symbol) == {"L/USDT", "S4/USDT"}
    assert not by_symbol["L/USDT"]["same_close"] and by_symbol["L/USDT"]["holders"] == ["momentum@4h"] * 3
    assert by_symbol["S4/USDT"]["same_close"]  # lowest reward:risk at that close: out-ranked
    assert sorted(by_symbol["S4/USDT"]["winners"]) == ["breakout@1d", "breakout@1d", "momentum@1d"]
    closes = {"BTC/USDT": pd.Series(np.linspace(100, 120, 60), index=idx),
              "ETH/USDT": pd.Series(np.linspace(50, 60, 60), index=idx)}
    text = format_portfolio_backtest(portfolio_backtest(closes, held + [late] + same, BotConfig(),
                                                        capital=1000, fraction=0.65, since=None))
    assert "Why slots were full" in text and "slots held by momentum@4h 100%" in text


def test_shared_close_follows_the_live_scan_order():
    """At 00:00 UTC the live bot scans 4h before 1d, and breaks rank ties by the 24h volume known then."""
    from tradebot.portfolio import simulate_satellite

    idx = pd.date_range("2022-01-01", periods=30, freq="D", tz="UTC")
    four_h = trade("ZZZ/USDT", 5, 10, 100.0, 101.0)
    four_h.timeframe, four_h.signal_rr = "4h", 2.0  # low reward:risk, but its scan runs first
    daily = []
    for s in ("AAA/USDT", "BBB/USDT", "CCC/USDT"):
        t = trade(s, 5, 10, 100.0, 101.0)
        t.strategy, t.signal_rr = "momentum", 8.0
        daily.append(t)
    for t, vol in zip(daily, (1e6, 5e6, 9e6)):  # 24h volume at the signal candle: CCC > BBB > AAA
        t.signal_volume = vol
    run = simulate_satellite([four_h] + daily, BotConfig(), idx, open_risk_pct=None)
    assert {t.symbol for t in run.taken} == {"ZZZ/USDT", "CCC/USDT", "BBB/USDT"}
    assert [d["trade"].symbol for d in run.skip_detail] == ["AAA/USDT"]


def test_report_compares_each_window_including_from_the_satellites_first_trade():
    from tradebot.portfolio import worst_dip

    eq = daily([100, 120, 90, 95, 130, 110])
    dip, top, low = worst_dip(eq)
    assert dip == pytest.approx(25.0) and top == eq.index[1] and low == eq.index[2]

    idx = pd.date_range("2021-01-01", periods=700, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    closes = {"BTC/USDT": pd.Series(100 * np.cumprod(1 + rng.normal(0.001, 0.03, 700)), index=idx),
              "ETH/USDT": pd.Series(50 * np.cumprod(1 + rng.normal(0.001, 0.04, 700)), index=idx)}
    trades = [trade("SOL/USDT", d, d + 8, 10.0, 10.6, start="2021-01-01") for d in range(400, 690, 6)]
    res = portfolio_backtest(closes, trades, BotConfig(), capital=1000, fraction=0.65, since="2021-06-01")
    text = format_portfolio_backtest(res)
    assert "dip low" in text and text.count("Rule (65/35 must beat core at same exposure") == 3
    assert "From the satellite's first trade (2022-02-05) - THE WINDOW THAT MEASURES THE SATELLITE" in text
    assert text.count("Break-even (all in this window)") == 1


def test_rule_line_says_when_dips_predate_the_satellite():
    idx = pd.date_range("2021-01-01", periods=700, freq="D", tz="UTC")
    crash = np.r_[np.linspace(100, 200, 100), np.linspace(200, 80, 60), np.linspace(80, 300, 540)]  # dip in 2021
    closes = {"BTC/USDT": pd.Series(crash, index=idx), "ETH/USDT": pd.Series(crash / 2, index=idx)}
    trades = [trade("SOL/USDT", d, d + 8, 10.0, 10.6, start="2021-01-01") for d in range(500, 690, 6)]
    text = format_portfolio_backtest(portfolio_backtest(closes, trades, BotConfig(), capital=1000, fraction=0.65,
                                                        since=None))
    assert "this dip comparison does not measure the satellite" in text
