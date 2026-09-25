import numpy as np
import pytest

from tradebot.backtest.engine import (Costs, Trade, backtest, portfolio_simulation,
                                      reward_risk, simulate_trade)
from tradebot.backtest.metrics import max_drawdown_pct, trade_metrics
from tradebot.strategies import make_strategy

from .conftest import bars

COSTS = Costs(fee_rate=0.001, slippage_rate=0.0005)


def run(rows, side="long", sl=95.0, tp=110.0, max_hold=10, exits=None, be=0.0):
    df = bars(rows)
    o, h, l, c = (df[k].to_numpy() for k in ("open", "high", "low", "close"))
    ex = None if exits is None else np.asarray(exits, dtype=bool)
    return simulate_trade(o, h, l, c, ex, 0, side, sl, tp, max_hold, COSTS, be)


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
