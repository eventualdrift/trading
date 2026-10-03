import numpy as np
import pytest

from tradebot.backtest.engine import (Costs, Trade, backtest, portfolio_simulation,
                                      reward_risk, simulate_trade)
from tradebot.backtest.metrics import max_drawdown_pct, trade_metrics
from tradebot.strategies import make_strategy

from .conftest import bars

COSTS = Costs(fee_rate=0.001, slippage_rate=0.0005)


def run(rows, side="long", sl=95.0, tp=110.0, max_hold=10, exits=None, be=0.0, trail=0.0):
    df = bars(rows)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ex = None if exits is None else np.asarray(exits, dtype=bool)
    return simulate_trade(o, h, l, c, ex, 0, side, sl, tp, max_hold, COSTS, be, trail)


BASE = [(100, 101, 99, 100), (100, 102, 99, 101)]


def test_take_profit_long_with_fees():
    out = run(BASE + [(101, 111, 100, 110)])
    entry = 100 * 1.0005
    assert out.reason == "take_profit" and out.exit_idx == 2
    assert out.entry_price == pytest.approx(entry)
    exit_ = 110 * (1 - 0.0005)  # the bot sells at market once the target trades
    assert out.exit_price == pytest.approx(exit_)
    net = (exit_ * 0.999 - entry * 1.001) / entry
    assert out.return_pct == pytest.approx(net)
    assert out.r_multiple == pytest.approx(net * entry / (entry - 95))


def test_stop_wins_when_both_hit_same_bar():
    out = run(BASE + [(101, 111, 94, 100)])
    assert out.reason == "stop_loss"
    assert out.exit_price == pytest.approx(95 * (1 - 0.0005))
    assert out.r_multiple < -1  # a full stop plus costs


def test_gap_through_stop_fills_at_open():
    out = run(BASE + [(90, 91, 89, 90)])
    assert out.reason == "stop_loss"
    assert out.exit_price == pytest.approx(90 * (1 - 0.0005))


def test_skip_if_entry_gaps_past_stop():
    assert run([(100, 101, 99, 100), (94, 95, 93, 94)]) is None
    assert run([(100, 101, 99, 100), (111, 112, 110, 111)]) is None


def test_time_stop():
    out = run(BASE + [(101, 102, 100, 101)] * 5, max_hold=2)
    assert out.reason == "time_stop" and out.exit_idx == 2
    assert out.exit_price == pytest.approx(101 * (1 - 0.0005))


def test_exit_signal():
    rows = BASE + [(101, 102, 100, 102), (102, 103, 101, 102)]
    out = run(rows, exits=[False, False, True, False])
    assert out.reason == "exit_signal" and out.exit_idx == 2
    assert out.exit_price == pytest.approx(102 * (1 - 0.0005))


def test_breakeven_stop():
    rows = BASE + [(101, 106, 100, 104), (104, 104, 100, 101)]
    out = run(rows, be=1.0)
    entry = 100 * 1.0005
    assert out.reason == "breakeven_stop"
    assert out.exit_price == pytest.approx(entry * (1 - 0.0005))
    assert -0.1 < out.r_multiple < 0  # only costs lost
    # without breakeven the same path is still open
    assert run(rows, be=0.0).reason == "end_of_data"


def test_target_wick_that_reverses_is_not_a_fill():
    """Review: open 100, high 111, close 100 - a polling bot would likely never see 110."""
    out = run(BASE + [(100, 111, 99.5, 100), (100, 100.5, 99.5, 100)], max_hold=10)
    assert out.reason != "take_profit"


def test_breakeven_bar_closing_below_entry_exits_at_close():
    out = run(BASE + [(101, 106, 100, 99.5)], be=1.0)
    assert out.reason == "breakeven_stop" and out.exit_idx == 2
    assert out.exit_price == pytest.approx(99.5 * (1 - 0.0005))


def test_short_take_profit():
    rows = BASE + [(100, 100.5, 89, 90)]
    out = run(rows, side="short", sl=105, tp=90)
    entry = 100 * (1 - 0.0005)
    assert out.reason == "take_profit"
    net = (entry * 0.999 - 90 * (1 + 0.0005) * 1.001) / entry
    assert out.return_pct == pytest.approx(net)


def test_incomplete_trade_flagged():
    out = run(BASE + [(101, 102, 100, 101)], max_hold=10)
    assert out.reason == "end_of_data" and not out.complete


def test_reward_risk():
    assert reward_risk("long", 100, 95, 110) == pytest.approx(2.0)
    assert reward_risk("short", 100, 105, 90) == pytest.approx(2.0)
    assert reward_risk("long", 100, 100, 110) == 0.0


def test_backtest_trades_do_not_overlap(ohlcv_1h):
    trades, _ = backtest(ohlcv_1h, make_strategy("breakout"), COSTS)
    assert len(trades) > 5
    for a, b in zip(trades, trades[1:]):
        assert b.signal_time >= a.exit_time
        assert b.entry_time > a.signal_time


def _trade(sym, entry_h, exit_h, ret=0.02, stop=0.02):
    import pandas as pd
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    return Trade(sym, "1h", "x", "long", 0, t0, t0 + pd.Timedelta(hours=entry_h), t0 + pd.Timedelta(hours=exit_h),
                 100, 102, 98, 104, "take_profit", exit_h - entry_h, ret / stop, ret, stop)


def test_portfolio_respects_limits():
    trades = [_trade("A", 1, 10), _trade("B", 2, 10), _trade("C", 3, 10), _trade("A", 4, 12), _trade("D", 11, 15)]
    curve, taken = portfolio_simulation(trades, risk_per_trade_pct=1, max_position_pct=100, max_open_positions=2)
    assert [t.symbol for t in taken] == ["A", "B", "D"]
    # each trade risks 1% of *realised* equity and makes 1R: A and B are sized off 1000,
    # D is sized off 1020 after both closed.
    assert curve.iloc[-1] == pytest.approx(1000 + 10 + 10 + 10.2)


def test_portfolio_position_cap():
    t = _trade("A", 1, 2, ret=0.01, stop=0.001)  # very tight stop -> huge size, capped
    curve, _ = portfolio_simulation([t], risk_per_trade_pct=1, max_position_pct=30, max_open_positions=3)
    assert curve.iloc[-1] == pytest.approx(1000 * (1 + 0.30 * 0.01))


def test_trade_metrics():
    m = trade_metrics([2, -1, 2, -1, -1])
    assert m["trades"] == 5
    assert m["win_rate"] == pytest.approx(0.4)
    assert m["expectancy_r"] == pytest.approx(0.2)
    assert m["profit_factor"] == pytest.approx(4 / 3)


def test_max_drawdown():
    import pandas as pd
    assert max_drawdown_pct(pd.Series([100, 120, 90, 130])) == pytest.approx(25.0)
    assert max_drawdown_pct(pd.Series([100.0, 110.0])) == 0.0


def test_trailing_stop_locks_in_gains():
    # entry 100.05, stop 95 (risk 5.05): +1R at 105.1 activates, then the stop trails 3 below the high
    rows = BASE + [(101, 106, 100.5, 105), (105, 110, 104, 109), (109, 109, 106, 106.5)]
    out = run(rows, tp=150, be=1.0, trail=3.0)
    assert out.reason == "trailing_stop" and out.exit_idx == 4
    assert out.exit_price == pytest.approx(107 * (1 - 0.0005))  # 110 high - 3
    assert out.r_multiple > 1.0


def test_trailing_close_check_on_the_moving_bar():
    rows = BASE + [(101, 110, 100.5, 106)]  # trail -> 107, but the bar closes at 106
    out = run(rows, tp=150, be=1.0, trail=3.0)
    assert out.reason == "trailing_stop" and out.exit_price == pytest.approx(106 * (1 - 0.0005))


def test_limit_entry_fills_only_when_price_trades_through():
    costs = Costs(fee_rate=0.001, slippage_rate=0.0005, maker_fee_rate=0.00075, limit_entry=True)
    df = bars([(100, 101, 99, 100), (100.5, 102, 99.9, 101), (101, 111, 100.5, 110)])
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    out = simulate_trade(o, h, l, c, None, 0, "long", 95.0, 110.0, 10, costs)
    assert out.entry_price == 100.0  # the limit itself: no slippage
    net = (110 * (1 - 0.0005) * (1 - 0.001) - 100 * (1 + 0.00075)) / 100  # maker in, taker out
    assert out.return_pct == pytest.approx(net)
    touch = bars([(100, 101, 99, 100), (100.5, 102, 100.0, 101)])  # only touches 100: no fill
    o, h, l, c = (touch[k].to_numpy() for k in ("open", "high", "low", "close"))
    assert simulate_trade(o, h, l, c, None, 0, "long", 95.0, 110.0, 10, costs) is None


def test_protection_is_recorded_when_the_stop_reaches_entry():
    # entry ~100.05, risk ~5.05: +1R is ~105.1, reached on bar 3; the trade runs on after that
    rows = BASE + [(101, 103, 100, 102), (102, 106, 101, 105), (105, 107, 104, 106), (106, 108, 105, 107)]
    out = run(rows, be=1.0, tp=200.0)
    assert out.protected_idx == 3 and out.reason == "end_of_data"
    assert run(BASE + [(101, 103, 100, 102)] * 4, be=1.0, tp=200.0).protected_idx is None  # never +1R


def test_trades_carry_the_24h_volume_known_at_the_signal():
    from tradebot.backtest.engine import backtest_populated

    df = bars([(100, 101, 99, 100)] * 60, freq="4h")
    df["volume"] = np.arange(60, dtype=float)  # growing volume: later candles trade more
    pop = df.assign(enter_long=False, enter_short=False, exit_long=False, exit_short=False,
                    long_sl=95.0, long_tp=120.0, short_sl=np.nan, short_tp=np.nan)
    pop.loc[pop.index[30], "enter_long"] = True

    class S:
        name, warmup, max_hold_bars = "x", 0, 5

    t = backtest_populated(pop, S(), COSTS, symbol="A/USDT", timeframe="4h")[0]
    # 24h of 4h candles = the signal candle and the 5 before it (volumes 25..30), at price 100
    assert t.signal_volume == pytest.approx(100 * sum(range(25, 31)))


# ------------------------------------------------- account replays: candidates and flat times
def _pop(rows, flags, sl=95.0, tp=110.0, start="2024-01-01", freq="1h"):
    import pandas as pd

    df = bars(rows, start=start, freq=freq)
    n = len(df)
    f = np.zeros(n, dtype=bool)
    f[list(flags)] = True
    return df.assign(enter_long=f, enter_short=False, exit_long=False, exit_short=False,
                     long_sl=sl, long_tp=tp, short_sl=np.nan, short_tp=np.nan).astype({"enter_short": bool}), pd


class _Strat:
    name, warmup, max_hold_bars = "x", 0, 50


FLAT = (100, 101, 99, 100)


def test_flat_time_says_when_the_slot_is_free():
    from tradebot.backtest.engine import backtest_populated

    hour = np.timedelta64(1, "h")
    for exit_bar, exit_at, extra in (((101, 103, 94, 100), "intrabar", 1),  # stop touched inside the candle
                                     ((90, 91, 89, 90), "open", 0),  # gapped through the stop at the open
                                     ((101, 111, 100, 110), "close", 1)):  # target: decided at the close
        pop, _ = _pop([FLAT, FLAT, exit_bar, FLAT], [0])
        (t,) = backtest_populated(pop, _Strat(), COSTS, symbol="A", timeframe="1h")
        assert t.exit_at == exit_at and t.exit_time == pop.index[2]
        assert t.flat_time == pop.index[2] + extra * hour


def test_candidates_include_signals_the_strategy_alone_would_skip():
    from tradebot.backtest.engine import backtest_populated, candidate_trades

    pop, _ = _pop([FLAT] * 12, [0, 3])  # the second signal comes while the first trade is still open
    alone = backtest_populated(pop, _Strat(), COSTS, symbol="A", timeframe="1h")
    every = candidate_trades(pop, _Strat(), COSTS, symbol="A", timeframe="1h")
    assert [t.signal_idx for t in alone] == [0] and [t.signal_idx for t in every] == [0, 3]


def test_a_skipped_trade_no_longer_blocks_its_coins_next_signal():
    """One slot. A on coin A takes it; B's first signal is skipped; B's second signal, while B's
    skipped trade would still have been running, is taken once A is flat - as live does."""
    import pandas as pd

    from tradebot.backtest.engine import candidate_trades
    from tradebot.config import BotConfig
    from tradebot.portfolio import simulate_satellite

    a, _ = _pop([FLAT] * 3 + [(100, 111, 100, 110)] + [FLAT] * 20, [0])  # A: in at 1h, target at 3h close
    b, _ = _pop([FLAT] * 24, [1, 6], tp=200.0)  # B: signals at 1h and 6h; each runs to the time stop
    cands = (candidate_trades(a, _Strat(), COSTS, symbol="A/USDT", timeframe="1h")
             + candidate_trades(b, _Strat(), COSTS, symbol="B/USDT", timeframe="1h"))
    cfg = BotConfig()
    cfg.risk.max_open_positions = 1
    run = simulate_satellite(cands, cfg, pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"))
    assert [(t.symbol, t.signal_idx) for t in run.taken] == [("A/USDT", 0), ("B/USDT", 6)]
    assert [(t.symbol, t.signal_idx) for t in run.skipped["max open positions (1)"]] == [("B/USDT", 1)]


def test_a_position_holds_its_slot_until_its_exit_candle_closes():
    import pandas as pd

    from tradebot.config import BotConfig
    from tradebot.portfolio import simulate_satellite

    held, new = _trade("A", 1, 5), _trade("B", 5, 9)  # A exits during the candle in which B would enter
    held.flat_time = held.exit_time + pd.Timedelta(hours=1)
    cfg = BotConfig()
    cfg.risk.max_open_positions = 1
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC")
    _, taken = portfolio_simulation([held, new], risk_per_trade_pct=1, max_position_pct=100, max_open_positions=1)
    assert [t.symbol for t in taken] == ["A"] == [t.symbol for t in simulate_satellite([held, new], cfg, idx).taken]
    held.flat_time = held.exit_time  # a gap through the stop at that candle's open: flat before B enters
    _, taken = portfolio_simulation([held, new], risk_per_trade_pct=1, max_position_pct=100, max_open_positions=1)
    assert [t.symbol for t in taken] == ["A", "B"] == [t.symbol for t in simulate_satellite([held, new], cfg, idx).taken]


def test_a_stop_inside_a_daily_candle_frees_the_slot_at_the_finer_candle():
    import pandas as pd

    from tradebot.backtest.engine import backtest_populated, refine_flat_times

    day = [FLAT, FLAT, (100, 101, 90, 96), FLAT]  # stop (95) touched on day 2
    pop, _ = _pop(day, [0], start="2024-01-01", freq="1D")
    (t,) = backtest_populated(pop, _Strat(), COSTS, symbol="A", timeframe="1d")
    assert t.exit_at == "intrabar" and t.flat_time == pd.Timestamp("2024-01-04", tz="UTC")
    four = bars([(100, 101, 99, 100)] * 13 + [(99, 100, 94.5, 95)] + [(96, 97, 95, 96)] * 4, start="2024-01-01",
                freq="4h")  # the 4h candle from 2024-01-03 04:00 reaches the stop
    refine_flat_times([t], {"1d": {"A": pop}, "4h": {"A": four}}, COSTS.slippage_rate)
    assert t.flat_time == pd.Timestamp("2024-01-03 08:00", tz="UTC")
