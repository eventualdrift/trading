import numpy as np
import pandas as pd
import pytest

from tradebot import indicators as ta


def test_rsi_bounds_and_extremes(ohlcv_1h):
    r = ta.rsi(ohlcv_1h["close"]).dropna()
    assert r.between(0, 100).all()
    up = pd.Series(np.arange(1.0, 60.0))
    assert ta.rsi(up).iloc[-1] == pytest.approx(100.0)
    down = pd.Series(np.arange(60.0, 1.0, -1.0))
    assert ta.rsi(down).iloc[-1] == pytest.approx(0.0)


def test_atr_positive(ohlcv_1h):
    assert (ta.atr(ohlcv_1h).dropna() > 0).all()


def test_ema_matches_pandas(ohlcv_1h):
    c = ohlcv_1h["close"]
    expected = c.ewm(span=20, adjust=False).mean()
    got = ta.ema(c, 20)
    assert np.allclose(got.iloc[20:], expected.iloc[20:])


@pytest.mark.parametrize("k", [300, 1000, 2500])
def test_indicators_are_causal(ohlcv_1h, k):
    """Values at bar k-1 must not change when later bars are appended."""
    full, part = ohlcv_1h, ohlcv_1h.iloc[:k]
    pairs = [
        (ta.ema(full["close"], 50), ta.ema(part["close"], 50)),
        (ta.rsi(full["close"]), ta.rsi(part["close"])),
        (ta.atr(full), ta.atr(part)),
        (ta.adx(full)[0], ta.adx(part)[0]),
        (ta.bollinger(full["close"])[2], ta.bollinger(part["close"])[2]),
        (ta.donchian(full, 20)[0], ta.donchian(part, 20)[0]),
    ]
    for a, b in pairs:
        assert a.iloc[k - 1] == pytest.approx(b.iloc[-1])


def test_donchian_excludes_current_bar(ohlcv_1h):
    upper, _ = ta.donchian(ohlcv_1h, 20)
    expected = ohlcv_1h["high"].iloc[100:120].max()
    assert upper.iloc[120] == pytest.approx(expected)
