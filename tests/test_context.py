import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.selection import ComboResult, evaluate_with_btc_filter
from tradebot.context import MarketContext, apply_vol_breaker
from tradebot.data.synthetic import generate_ohlcv
from tradebot.scanner import is_frozen
from tradebot.strategies import make_strategy


def daily(closes, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(closes), freq="D", tz="UTC", name="time")
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)


def test_uptrend_is_known_only_after_the_daily_close():
    # 5-day average; price jumps on day 10 (2024-01-11) -> uptrend from that day's close
    closes = [100.0] * 10 + [120.0] * 5
    ctx = MarketContext(daily(closes), None, uptrend_days=5)
    hours = pd.date_range("2024-01-11 00:00", periods=26, freq="h", tz="UTC")
    al = ctx.align(hours, "1h")["btc_uptrend"]
    assert al.loc["2024-01-11 22:00"] == 0.0  # closes 23:00 on the 11th: daily bar not closed yet
    assert al.loc["2024-01-11 23:00"] == 1.0  # closes at 00:00 on the 12th, together with the daily bar
    assert al.loc["2024-01-12 01:00"] == 1.0


def test_uptrend_unknown_before_enough_history():
    ctx = MarketContext(daily([100.0] * 3), None, uptrend_days=5)
    assert np.isnan(ctx.align(pd.date_range("2024-01-02", periods=2, freq="D", tz="UTC"), "1d")["btc_uptrend"]).all()
    assert ctx.uptrend_now() is None


def test_vol_ratio_and_breaker():
    idx = pd.date_range("2024-01-01", periods=300, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    rets = np.concatenate([rng.normal(0, 0.002, 280), rng.normal(0, 0.03, 20)])  # burst at the end
    c = 100 * np.exp(np.cumsum(rets))
    hourly = pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)
    ctx = MarketContext(None, hourly)
    assert ctx.vol_ratio_now() > 2.5  # reachable: the long window excludes the burst
    pop = pd.DataFrame({"enter_long": True, "enter_short": False}, index=idx)
    out = apply_vol_breaker(pop.copy(), ctx, "1h", 2.5)
    assert out["enter_long"].iloc[150] and not out["enter_long"].iloc[-1]


def test_btc_filter_blocks_entries_outside_uptrends():
    df = generate_ohlcv(3000, "1h", seed=11)
    plain = make_strategy("breakout").populate(df)
    assert plain["enter_long"].sum() > 5
    down = MarketContext(daily(list(np.linspace(200, 100, 200))), None, uptrend_days=20)
    filtered = make_strategy("breakout", {"btc_filter": True}).populate(df, down, "1h")
    assert filtered["enter_long"].sum() == 0
    assert make_strategy("breakout", {"btc_filter": True}).populate(df)["enter_long"].sum() == 0  # no context


def _combo(exp_is, exp_oos, selected=True, params=None):
    m = lambda e: {"trades": 50, "expectancy_r": e}  # noqa: E731
    return ComboResult("breakout", "1d", params or {}, m(exp_is), m(exp_oos), 5, 1.0, selected)


@pytest.mark.parametrize("filtered, keep", [
    ((0.135, 0.196), True),   # better in both periods (the local research result)
    ((0.150, 0.100), False),  # better in-sample only
    ((0.100, 0.250), False),  # better out-of-sample only
])
def test_auto_keeps_filter_only_if_both_periods_improve(monkeypatch, cfg, filtered, keep):
    import tradebot.backtest.selection as sel

    cfg.selection.btc_filter = "auto"

    def fake(datasets, name, params, tf, cfg, context=None):
        if params.get("btc_filter"):
            return _combo(*filtered, params=params)
        return _combo(0.127, 0.161, params=params)

    monkeypatch.setattr(sel, "evaluate_combo", fake)
    res = evaluate_with_btc_filter({}, "breakout", {}, "1d", cfg, context=object())
    assert bool(res.params.get("btc_filter")) is keep
    assert res.notes and res.notes[0].startswith("BTC-uptrend filter kept") is keep


def test_frozen_feed_detection():
    df = generate_ohlcv(50, "1h", seed=1)
    assert not is_frozen(df, 6)
    frozen = df.copy()
    frozen.iloc[-6:, :4] = 100.0
    frozen.iloc[-6:, 4] = 0.0
    assert is_frozen(frozen, 6)
    frozen.iloc[-1, 4] = 5.0  # some volume traded: a genuinely quiet market, not a dead feed
    assert not is_frozen(frozen, 6)


def test_breaker_study_reports_baseline_and_ratios(cfg):
    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.research import breaker_study, format_breaker_study
    from tradebot.timeframes import resample_ohlcv

    base = generate_ohlcv(24 * 4 * 400, "15m", seed=5)
    ctx = MarketContext(resample_ohlcv(base, "1d"), resample_ohlcv(base, "1h"))
    sel = Selection(0.0, [ComboResult("breakout", "1h", {}, {}, {}, 1, 1.0, True)])
    datasets = {"1h": {"BTC/USDT": resample_ohlcv(base, "1h")}}
    rows = breaker_study(sel, datasets, cfg, ctx, ratios=(1.5, 3.0))
    assert [r.ratio for r in rows] == [None, 1.5, 3.0]
    assert rows[1].is_trades + rows[1].oos_trades <= rows[0].is_trades + rows[0].oos_trades  # only removes trades
    assert "Recommendation" in format_breaker_study(rows)
