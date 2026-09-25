import pandas as pd
import pytest

from tradebot.backtest.engine import Costs
from tradebot.backtest.selection import ComboResult, Selection
from tradebot.bot import TradingBot
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.models import Position, Signal
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
MIN = 60_000


def managed(cfg, rows, price=100.0, max_hold=T0 + DAY, broker=None):
    market = StubMarket(rows, T0, price)
    db = Database(cfg.state_path / "m.db")
    broker = broker or PaperBroker(db, Costs(0.0, 0.0), 1000.0)
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=selection())
    sig = Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, 110.0, 0, T0, T0, max_hold)
    sig.id = 1
    pos = Position.from_signal(sig, broker.mode, 1.0, T0)
    db.insert_position(pos)
    broker.open_position(pos, 100.0, T0)
    pos.last_checked_ms = T0
    db.update_position(pos)
    return market, db, notes, bot


def test_take_profit_needs_the_current_price(cfg):
    """Review #7: a past wick through the target is not a fill - the bot sells at market now."""
    market, db, notes, bot = managed(cfg, [(100, 102, 99, 101), (101, 111, 100.5, 104)], price=104.0)
    bot.manage_positions(T0 + 2 * MIN + 1)
    [p] = db.open_positions("paper")  # wick to 111 then back to 104: no take-profit
    assert p.breakeven_moved
    market.price = 110.5
    bot.manage_positions(T0 + 2 * MIN + 30_000)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "take_profit" and p.exit_price == pytest.approx(110.5)
    assert any("Take-profit" in m for m in notes.messages)


def test_wick_through_target_then_collapse_is_not_a_take_profit(cfg):
    """The reviewer's exact case: touch 110, close 99. Old code booked +2R at 110."""
    market, db, notes, bot = managed(cfg, [(100, 110, 99, 99)], price=99.0)
    bot.manage_positions(T0 + MIN + 1)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "breakeven_stop" and p.exit_price == pytest.approx(99.0)
    assert p.pnl < 0


def test_breakeven_move_then_stop(cfg):
    rows = [(100, 106, 99.5, 105), (105, 105, 99, 99.5)]
    market, db, notes, bot = managed(cfg, rows, price=99.5)
    bot.manage_positions(T0 + 2 * MIN + 1)
    p = db.closed_positions("paper")[0]
    assert p.breakeven_moved and p.exit_reason == "breakeven_stop"
    assert p.exit_price == pytest.approx(99.5)  # bot-managed exit: market price now
    assert any("Move stop to breakeven" in m for m in notes.messages)


def test_same_bar_cannot_trigger_its_own_breakeven_stop(cfg):
    """Review #8: +1R and a dip below entry inside ONE bar must not stop the trade out."""
    market, db, notes, bot = managed(cfg, [(100, 106, 99, 105)], price=104.0)
    bot.manage_positions(T0 + MIN + 1)  # bar complete: breakeven activates
    bot.manage_positions(T0 + MIN + 30_000)  # next poll must not re-read that bar
    [p] = db.open_positions("paper")
    assert p.breakeven_moved and p.stop_loss == 100.0
    assert p.last_checked_ms == T0 + MIN


def test_breakeven_from_live_price_ignores_earlier_part_of_the_bar(cfg):
    market, db, notes, bot = managed(cfg, [(100, 106, 99, 104)], price=105.0)
    bot.manage_positions(T0 + 30_000)  # mid-bar: current price is +1R
    assert db.open_positions("paper")[0].breakeven_moved
    market.price = 104.0
    bot.manage_positions(T0 + MIN + 1)  # the completed bar's earlier low of 99 must not count
    assert len(db.open_positions("paper")) == 1


def test_stop_loss_and_gap(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100), (90, 91, 89, 90)], price=90.0)
    bot.manage_positions(T0 + 2 * MIN + 1)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "stop_loss" and p.exit_price == 90.0  # gapped below the stop


def test_stop_loss_fills_at_the_level(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 94, 97)], price=97.0)
    bot.manage_positions(T0 + MIN + 1)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "stop_loss" and p.exit_price == 95.0  # exchange stop-market semantics


def test_time_stop(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100)], price=101.0, max_hold=T0 + 60_000)
    bot.manage_positions(T0 + 120_000)
    p = db.closed_positions("paper")[0]
    assert p.exit_reason == "time_stop" and p.exit_price == 101.0


def test_open_position_untouched_between_levels(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100), (100, 103, 98, 102), (102, 103, 101, 102)])
    bot.manage_positions(T0 + 2 * MIN + 30_000)  # third bar still forming
    [p] = db.open_positions("paper")
    assert p.last_checked_ms == T0 + 2 * MIN


def test_forget_command(cfg):
    market, db, notes, bot = managed(cfg, [(100, 101, 99, 100)])
    pid = db.open_positions("paper")[0].id
    assert "marked closed" in bot.handle_command("forget", [str(pid)])
    assert db.get_position(pid).exit_reason == "forgotten"


# ------------------------------------------------------------- live broker in the loop
def live_bot(cfg, prepare=None):
    from tradebot.execution import LiveBroker

    from .test_brokers import FakeClient

    client = FakeClient("okx")
    if prepare:
        prepare(client.ex)
    broker = LiveBroker(client, "USDT", sleep=lambda s: None, fill_timeout_s=2)
    market = StubMarket([(100, 101, 99, 100)], T0, 100.0)
    db = Database(cfg.state_path / "live.db")
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=selection())
    sig = Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, 110.0, T0 - 3_600_000, T0, T0 + 3_600_000,
                 T0 + DAY, reason="test")
    return client, db, notes, bot, sig


def test_live_entry_is_protected_and_recorded(cfg):
    client, db, notes, bot, sig = live_bot(cfg)
    pos = bot.handle_signal(sig, T0 + 30_000, 1000.0)
    assert pos is not None and pos.status == "open" and pos.sl_order_id
    stored = db.get_position(pos.id)
    assert stored.status == "open" and stored.sl_order_id == pos.sl_order_id
    assert client.ex.orders[pos.sl_order_id]["status"] == "open"
    assert any("BUY SIGNAL" in m for m in notes.messages)


def test_live_entry_filled_but_stop_times_out_is_not_lost(cfg):
    """Review #3/#4: the buy fills, the stop times out -> the position is recorded and,
    since it's unprotected, sold for safety."""
    import ccxt

    client, db, notes, bot, sig = live_bot(cfg, lambda ex: ex.fail_next.update(create_stop=ccxt.RequestTimeout("timeout")))
    bot.handle_signal(sig, T0 + 30_000, 1000.0)
    [p] = db.closed_positions("live")
    assert p.exit_reason == "no_protection"
    assert client.ex.free["BTC"] == pytest.approx(0.0)
    assert any("No exchange stop-loss" in m for m in notes.messages)


def test_live_unprotected_can_be_kept_if_configured(cfg):
    cfg.live.require_exchange_stop = False
    cfg.live.native_stop_loss = True
    client, db, notes, bot, sig = live_bot(cfg, lambda ex: setattr(ex, "stop_mode", "reject_all"))
    bot.handle_signal(sig, T0 + 30_000, 1000.0)
    [p] = db.open_positions("live")
    assert p.sl_order_id is None
    assert any("enforces the stop itself" in m for m in notes.messages)


def test_live_unknown_entry_halts_and_is_kept(cfg):
    import ccxt

    client, db, notes, bot, sig = live_bot(cfg, lambda ex: ex.fail_next.update(create_buy=ccxt.RequestTimeout("t")))
    assert bot.handle_signal(sig, T0 + 30_000, 1000.0) is None
    [p] = db.positions_with_status("live", ("unknown",))
    assert p.client_order_id and db.kv_get("live:halted")
    assert any("New entries halted" in m for m in notes.messages)


def test_startup_reconciliation(cfg):
    client, db, notes, bot, sig = live_bot(cfg)
    stuck = Position.from_signal(sig, "live", 1.0, T0)
    db.insert_position(stuck)  # crashed between recording and confirming the entry
    bot.check_unresolved()
    assert db.get_position(stuck.id).status == "unknown"
    assert db.kv_get("live:halted")


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
        if t.reason in ("take_profit", "breakeven_stop"):
            # bot-managed market exits fill at the price when the bot looks (here: the next
            # 15-minute close), the backtest at the level itself
            assert p.r_multiple == pytest.approx(t.r_multiple, abs=0.35), key
        else:  # stops, exit signals and time stops fill identically
            assert p.r_multiple == pytest.approx(t.r_multiple, abs=0.02), key
