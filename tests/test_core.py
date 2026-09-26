import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.engine import Costs
from tradebot.backtest.selection import Selection
from tradebot.bot import TradingBot
from tradebot.config import CoreConfig
from tradebot.core import CoreSleeve, simulate_core, trend_weight
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.notify import MemoryNotifier

DAY = 86_400_000


def test_trend_weight_counts_averages_below_the_close():
    closes = pd.Series(np.concatenate([np.full(150, 100.0), np.full(50, 110.0)]))
    # last close 110: above the 150/200-day averages (they include the 100s), equal to the 50-day one
    assert trend_weight(closes, [50, 100, 150, 200]) == pytest.approx(0.75)
    assert trend_weight(closes.iloc[:100], [50, 100, 150, 200]) is None  # not enough history


@pytest.fixture
def core_bot(cfg):
    cfg.core.fraction = 0.65
    cfg.telegram.daily_summary_hour_utc = 5
    market = SyntheticMarket(["BTC/USDT", "ETH/USDT"], days=420, base_tf="1h", seed=21)
    db = Database(cfg.state_path / "core.db")
    broker = PaperBroker(db, Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate), 1000.0, market)
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=Selection(0.0, []))
    return market, db, broker, notes, bot


def run_days(market, bot, start_day, days):
    for d in range(start_day, start_day + days):
        now = market.start_ms + d * DAY + 30_000  # 30s after each daily close
        market.set_now(now)
        bot.tick(now)


def test_core_sleeve_starts_and_matches_the_backtest(core_bot, cfg):
    market, db, broker, notes, bot = core_bot
    cfg.core.rebalance_sleeves_days = 0  # compare the core alone, without capital moving between sleeves
    run_days(market, bot, 150, 250)
    assert bot.core.initialized and bot.core.contributed == pytest.approx(650.0)
    assert broker.cash == pytest.approx(350.0)  # satellite kept the rest (no satellite trades)
    assert any("Core sleeve started" in m for m in notes.messages)
    assert any("Core rebalance" in m for m in notes.messages)
    assert bot.core.cash >= 0
    # the live loop and simulate_core must agree (same rules, same fees)
    closes = {s: market.fetch_ohlcv_df(s, "1d", limit=10_000)["close"] for s in ("BTC/USDT", "ETH/USDT")}
    sim = simulate_core(closes, cfg.core, Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate), start_equity=650.0)
    prices = {s: market.fetch_last_price(s) for s in closes}
    assert bot.core.equity(prices) == pytest.approx(sim.iloc[-1], rel=1e-3)
    assert len(db.core_trades("paper", 1000)) >= 5  # it did trade


def test_core_rebalances_rarely_and_in_steps(core_bot):
    market, db, broker, notes, bot = core_bot
    run_days(market, bot, 150, 250)
    trades = db.core_trades("paper", 1000)
    days_active = 250 - 50  # weights exist from day ~200
    assert len(trades) < days_active  # not every day: only on weight steps / real drift
    for t in trades:
        if t["reason"] == "trend weights":
            assert t["weight_to"] in (0.0, 0.25, 0.5, 0.75, 1.0)


def test_sleeves_reset_to_target_split(core_bot, cfg):
    market, db, broker, notes, bot = core_bot
    cfg.core.rebalance_sleeves_days = 30
    run_days(market, bot, 150, 100)
    s = bot.sleeves(market.now_ms())
    assert s["core"] / s["total"] == pytest.approx(0.65, abs=0.1)
    assert any("Sleeves reset" in m for m in notes.messages) or abs(s["core"] / s["total"] - 0.65) < 0.01


def test_core_start_does_not_trip_satellite_breakers(core_bot):
    market, db, broker, notes, bot = core_bot
    run_days(market, bot, 150, 3)
    assert not db.kv_get("paper:halted")  # 65% moved out of the satellite is not a 65% loss
    assert not any("halted" in m.lower() for m in notes.messages)


def test_status_shows_sleeves(core_bot):
    market, db, broker, notes, bot = core_bot
    run_days(market, bot, 150, 60)
    text = bot.handle_command("status", [])
    assert "Core:" in text and "Satellite:" in text and "Total equity" in text


def test_core_is_paper_only(tmp_path, monkeypatch):
    from tradebot.config import load_config

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("mode: live\ncore:\n  fraction: 0.65\n")
    with pytest.raises(ValueError, match="paper-only"):
        load_config(p, env_file=None)


def test_withdraw_sells_pro_rata_when_cash_is_short(tmp_path):
    db = Database(tmp_path / "w.db")
    core = CoreSleeve(db, CoreConfig(), Costs(0.001, 0.0005), "paper", market=None)
    core.initialize(1000.0)
    core._trade("BTC/USDT", "buy", 5.0, 100.0, 0, "test")
    core._trade("ETH/USDT", "buy", 4.0, 100.0, 0, "test")
    prices = {"BTC/USDT": 100.0, "ETH/USDT": 100.0}
    out, trades = core.withdraw(300.0, prices, 1)
    assert out == pytest.approx(300.0, rel=0.01) and len(trades) == 2 and core.cash >= 0


def test_report_shows_sleeve_pnl(core_bot, cfg):
    from tradebot.report import sleeve_summary

    market, db, broker, notes, bot = core_bot
    for d in range(150, 230):  # ticks every 6 hours so equity snapshots are recorded
        for h in (0, 6, 12, 18):
            now = market.start_ms + d * DAY + h * 3_600_000 + 30_000
            market.set_now(now)
            bot.tick(now)
    text = sleeve_summary(db, cfg, "paper")
    assert "Core" in text and "Satellite" in text and "Holding BTC" in text
