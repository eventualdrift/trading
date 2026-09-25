import numpy as np
import pytest

from tradebot.strategies import SIGNAL_COLUMNS, STRATEGIES, make_strategy


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_levels_are_consistent(name, ohlcv_1h):
    pop = make_strategy(name).populate(ohlcv_1h)
    for col in SIGNAL_COLUMNS:
        assert col in pop
    longs = pop[pop["enter_long"]]
    assert len(longs) > 0, "strategy should fire on 3000 bars of data"
    assert (longs["long_sl"] < longs["close"]).all()
    assert (longs["long_tp"] > longs["close"]).all()
    shorts = pop[pop["enter_short"]]
    assert (shorts["short_sl"] > shorts["close"]).all()
    assert (shorts["short_tp"] < shorts["close"]).all()


@pytest.mark.parametrize("name", sorted(STRATEGIES))
@pytest.mark.parametrize("k", [400, 1200, 2999])
def test_no_lookahead(name, k, ohlcv_1h):
    """The live bot only sees data up to now; signals must match the backtest."""
    strat = make_strategy(name)
    full = strat.populate(ohlcv_1h).iloc[k - 1][SIGNAL_COLUMNS]
    part = strat.populate(ohlcv_1h.iloc[:k]).iloc[-1][SIGNAL_COLUMNS]
    for col in SIGNAL_COLUMNS:
        a, b = full[col], part[col]
        if isinstance(a, (float, np.floating)) and np.isnan(a):
            assert np.isnan(b)
        else:
            assert a == pytest.approx(b), col


def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        make_strategy("trend", {"nope": 1})
    with pytest.raises(ValueError):
        make_strategy("does-not-exist")


def test_params_override():
    s = make_strategy("trend", {"rr": 3.0})
    assert s.params["rr"] == 3.0 and s.params["sl_atr"] == 2.0


def test_explain_mentions_indicators(ohlcv_1h):
    s = make_strategy("trend")
    pop = s.populate(ohlcv_1h)
    row = pop[pop["enter_long"]].iloc[0]
    text = s.explain(row, "long")
    assert "ADX" in text and "RSI" in text
