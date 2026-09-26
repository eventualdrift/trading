import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.engine import Trade
from tradebot.config import BotConfig
from tradebot.projection import equity_returns, format_projection, project


def trades(returns, days=90, stop_pct=0.02):
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    out = []
    step = pd.Timedelta(days=days) / max(len(returns), 1)
    for i, r in enumerate(returns):
        s = t0 + i * step
        out.append(Trade(f"C{i % 3}/USDT", "4h", "x", "long", i, s, s, s + step * 0.5, 100, 100 * (1 + r),
                         98, 104, "take_profit" if r > 0 else "stop_loss", 5, r / stop_pct, r, stop_pct))
    return out


def test_sizing_matches_the_live_risk_rules():
    cfg = BotConfig()
    [r] = equity_returns(trades([0.04]), cfg)  # risk 1% on a 2% stop -> 50% notional, capped at 30%
    assert r == pytest.approx(0.30 * 0.04)


def test_projection_is_ordered_and_sane():
    rng = np.random.default_rng(1)
    rets = list(np.where(rng.random(120) < 0.45, 0.04, -0.02))
    p = project(trades(rets), BotConfig(), capital=1000, runs=2000, seed=3)
    assert set(p.rows) == {1, 2, 3, 6, 12}
    for s in p.rows.values():
        assert s["p5"] <= s["p25"] <= s["median"] <= s["p75"] <= s["p95"]
        assert 0.0 <= s["p_loss"] <= 1.0
    assert p.trades_per_month == pytest.approx(len(p.rows) and p.trades / (p.span_days / 30.44))
    text = format_projection(p)
    assert "12 months" in text and "chance of loss" in text


def test_losing_trades_project_losses():
    p = project(trades([-0.02] * 60), BotConfig(), runs=500, seed=0)
    assert p.rows[3]["p_loss"] == 1.0 and p.rows[3]["p95"] < 1000


def test_benchmark_comparison():
    idx = pd.date_range("2024-01-01", periods=200, freq="D", tz="UTC")
    btc = pd.DataFrame({"close": np.linspace(100, 150, 200)}, index=idx)
    p = project(trades([0.04, -0.02] * 30), BotConfig(), runs=200, benchmark=btc, benchmark_symbol="BTC/USDT")
    assert p.benchmark_pct is not None and p.benchmark_pct > 0
    assert "Simply holding BTC/USDT" in format_projection(p)


def test_no_trades_is_an_error():
    with pytest.raises(ValueError):
        project([], BotConfig())
