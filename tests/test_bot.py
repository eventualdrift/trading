import pandas as pd
import pytest

from tradebot.backtest.engine import Costs
from tradebot.backtest.selection import ComboResult, Selection
from tradebot.bot import TradingBot
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.models import Signal
from tradebot.notify import MemoryNotifier
from tradebot.timeframes import index_ms

from .conftest import bars

DAY = 86_400_000
EXIT_REASONS = {"take_profit", "stop_loss", "breakeven_stop", "exit_signal", "time_stop"}


def selection(*keys):
    return Selection(0.0, [ComboResult(s, tf, {}, {}, {}, 1, 1.0, True) for s, tf in keys])


@pytest.fixture
def sim(cfg):
    market = SyntheticMarket(4, days=120, seed=3)
    db = Database(cfg.state_path / "t.db")
    broker = PaperBroker(db, Costs(), 1000.0, market)
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes,
                     selection=selection(("breakout", "1h"), ("trend", "4h")))
    return market, db, broker, notes, bot


def run(market, bot, start, days, each=None):
    step = market.price_bar_ms
    t = (start // step + 1) * step
    while t < start + days * DAY:
        now = t + 30_000
        market.set_now(now)
        bot.tick(now)
        if each:
            each()
        t += step


def test_bot_trades_within_risk_limits(sim, cfg):
    market, db, broker, notes, bot = sim

    def check():
        open_ = db.open_positions("paper")
        assert len(open_) <= cfg.risk.max_open_positions
        assert len({p.symbol for p in open_}) == len(open_)

    run(market, bot, market.start_ms + 45 * DAY, 40, check)
    closed = db.closed_positions("paper")
    assert len(closed) >= 5
    assert {p.exit_reason for p in closed} <= EXIT_REASONS
    for p in closed:
        if p.exit_reason == "take_profit":
            assert p.exit_price == pytest.approx(p.take_profit)
        # 1% risk per trade: no single loss should be far beyond 1% of the account (+costs/gaps)
        assert p.pnl > -0.03 * 1000
    assert any("SIGNAL" in m for m in notes.messages)
    assert any(" NOW " in m for m in notes.messages)
    # accounting invariant: cash = start + realised P&L - entry fees of still-open trades
    open_fees = sum(p.fees for p in db.open_positions("paper"))
    assert broker.cash == pytest.approx(1000 + sum(p.pnl for p in closed) - open_fees)
    assert db.recent_signals(1)


def test_pause_blocks_new_entries(sim):
    market, db, broker, notes, bot = sim
    assert "Paused" in bot.handle_command("pause", [])
    run(market, bot, market.start_ms + 45 * DAY, 12)
    assert db.open_positions("paper") == [] and db.closed_positions("paper") == []
    skipped = db.recent_signals(50, status="skipped")
    assert skipped and all(s.note == "paused by user" for s in skipped)


def test_commands(sim):
    market, db, broker, notes, bot = sim
    run(market, bot, market.start_ms + 45 * DAY, 6)
    assert "Commands" in bot.handle_command("help", [])
    assert "Equity" in bot.handle_command("status", [])
    assert "positions" in bot.handle_command("positions", []).lower()
    assert "Trades" in bot.handle_command("performance", []) or "No closed" in bot.handle_command("performance", [])
    assert "Unknown" in bot.handle_command("xyz", [])
    assert "Usage" in bot.handle_command("close", [])
    assert "No such" in bot.handle_command("close", ["99999"])


def test_closeall_kill_switch(sim):
    market, db, broker, notes, bot = sim
    start = market.start_ms + 45 * DAY
    step = market.price_bar_ms
    t = start
    while not db.open_positions("paper") and t < start + 60 * DAY:
        t += step
        market.set_now(t + 30_000)
        bot.tick(t + 30_000)
    assert db.open_positions("paper"), "expected at least one trade to open"
    bot.handle_command("closeall", [])
    assert db.open_positions("paper") == []
    assert all(p.exit_reason == "kill_switch" for p in db.closed_positions("paper")[-1:])
    assert db.kv_get("paper:paused") is True


def test_drawdown_halt(sim):
    market, db, broker, notes, bot = sim
    db.kv_set("paper:peak_equity", 2000.0)
    now = market.start_ms + 50 * DAY
    market.set_now(now)
    bot.tick(now)
    assert db.kv_get("paper:halted")
    assert any("Trading halted" in m for m in notes.messages)
    bot.handle_command("resume", [])
    assert not db.kv_get("paper:halted")


def test_stale_candles_are_not_traded_on_startup(sim, monkeypatch):
    market, db, broker, notes, bot = sim
    calls = []
    monkeypatch.setattr(bot.scanner, "scan", lambda *a, **k: calls.append(a) or __import__("tradebot.scanner", fromlist=["ScanResult"]).ScanResult())
    hour = 3_600_000
    t = (market.start_ms // hour + 24 * 50) * hour
    market.set_now(t + 40 * 60_000)  # 40 min after the close: stale for a 1h strategy
    bot.tick(t + 40 * 60_000)
    assert [c for c in calls if c[0] == "1h"] == []
    market.set_now(t + hour + 30_000)  # fresh close
    bot.tick(t + hour + 30_000)
    assert len([c for c in calls if c[0] == "1h"]) == 1


class StubMarket:
    id = "stub"
    price_bar_ms = 60_000

    def __init__(self, rows, start_ms, price):
        self.bars = bars(rows, start=pd.Timestamp(start_ms, unit="ms", tz="UTC"), freq="1min")
        self.price = price
        self.now = start_ms

    def now_ms(self):
        return self.now

    def fetch_price_bars(self, symbol, since_ms):
        return self.bars[index_ms(self.bars.index) >= since_ms]

    def fetch_last_price(self, symbol):
        return self.price

    def top_symbols(self, *a, **k):
        return []


T0 = 1_700_000_000_000 // 60_000 * 60_000


def managed(cfg, rows, price=100.0, max_hold=T0 + DAY):
    market = StubMarket(rows, T0, price)
    db = Database(cfg.state_path / "m.db")
    broker = PaperBroker(db, Costs(0.0, 0.0), 1000.0)
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=selection())
    sig = Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, 110.0, 0, T0, T0, max_hold)
    pos = broker.open_position(sig, 1.0, 100.0, T0)
    pos.last_checked_ms = T0
    db.insert_position(pos)
    return market, db, notes, bot


def test_take_profit_closes_at_level(cfg):
    market, db, notes, bot = managed(cfg, [(100, 102, 99, 101), (101, 111, 100, 109)])
    bot.manage_positions(T0 + 180_000)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "take_profit" and p.exit_price == 110.0 and p.pnl == pytest.approx(10.0)
    assert any("Take-profit" in m for m in notes.messages)


def test_breakeven_move_then_stop(cfg):
    rows = [(100, 106, 99, 105), (105, 105, 99, 99)]
    market, db, notes, bot = managed(cfg, rows)
    bot.manage_positions(T0 + 180_000)
    p = db.closed_positions("paper")[0]
    assert p.breakeven_moved and p.exit_reason == "breakeven_stop"
    assert p.pnl == pytest.approx(0.0)
    assert any("Move stop to breakeven" in m for m in notes.messages)


def test_stop_loss_and_gap(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100), (90, 91, 89, 90)])
    bot.manage_positions(T0 + 180_000)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "stop_loss" and p.exit_price == 90.0  # gapped below the stop


def test_time_stop(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100)], price=101.0, max_hold=T0 + 60_000)
    bot.manage_positions(T0 + 120_000)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "time_stop" and p.exit_price == 101.0


def test_open_position_untouched_between_levels(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100), (100, 103, 98, 102)])
    bot.manage_positions(T0 + 180_000)
    [p] = db.open_positions("paper")
    assert p.last_checked_ms == T0 + 60_000


def test_live_loop_matches_backtest(cfg):
    """The bot trading candle-by-candle must take the same trades the backtester simulates."""
    from tradebot.backtest.engine import backtest_populated
    from tradebot.strategies import make_strategy

    cfg.risk.max_open_positions = 10  # no portfolio constraint, so every signal is taken
    cfg.risk.max_total_exposure_pct = 1000
    cfg.risk.daily_loss_limit_pct = 0
    cfg.risk.max_drawdown_pct = 0
    market = SyntheticMarket(3, days=100, seed=9)
    db = Database(cfg.state_path / "p.db")
    bot = TradingBot(cfg, market, PaperBroker(db, Costs(), 1000.0, market), db, MemoryNotifier(),
                     selection=selection(("breakout", "1h")))
    end = market.now_ms()
    start = end - 40 * DAY
    run(market, bot, start, 39)

    hour = 3_600_000
    live = {(p.symbol, (p.opened_at - 30_000) // hour * hour - hour): p for p in db.closed_positions("paper")}
    strat = make_strategy("breakout")
    expected = {}
    for sym in market.symbols:
        df = market.fetch_ohlcv_df(sym, "1h", limit=100_000)
        for t in backtest_populated(strat.populate(df), strat, Costs(), symbol=sym,
                                    breakeven_at_r=cfg.risk.breakeven_at_r,
                                    min_reward_risk=cfg.risk.min_reward_risk):
            sig_ms = int(t.signal_time.value // 1_000_000)
            first_scan = (start // hour + 1) * hour  # the bot's first candle close
            if sig_ms + hour >= first_scan and t.exit_time.value // 1_000_000 + hour <= end - DAY:
                expected[(sym, sig_ms)] = t
    assert len(expected) >= 8
    assert set(expected) <= set(live)
    for key, t in expected.items():
        p = live[key]
        assert p.exit_reason == t.reason, key
        assert p.r_multiple == pytest.approx(t.r_multiple, abs=0.15), key
