"""The bot in testnet mode: the live broker's exchange-side exits driven by the real bot loop."""
import pytest

from tradebot.bot import TradingBot
from tradebot.db import Database
from tradebot.execution import LiveBroker
from tradebot.models import Position, Signal
from tradebot.notify import MemoryNotifier

from .fakespot import FakeSpotClient
from .test_bot import DAY, MIN, T0, StubMarket, selection
from .test_brokers import Clock


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr("tradebot.data.exchange.time.sleep", lambda s: None)


def make_testnet_bot(cfg, rows, price=100.0, core=0.0):
    cfg.core.fraction = core
    client, clock = FakeSpotClient({"BTC/USDT": price, "ETH/USDT": 10.0}), Clock()
    broker = LiveBroker(client, "USDT", sleep=clock.sleep, clock=clock, fill_timeout_s=2, testnet=True)
    market = StubMarket(rows, T0, price)
    db = Database(cfg.state_path / "testnet.db")
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=selection())
    return client, market, db, notes, bot


def signal(tp=150.0, trail=3.0):
    return Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, tp, T0 - 3_600_000, T0, T0 + 3_600_000, T0 + DAY,
                  reason="test", trail_distance=trail)


def test_the_bot_moves_the_exchange_stop_and_the_exchange_closes_the_trade(cfg):
    rows = [(100, 101, 99, 100), (100, 106, 100, 105), (105, 109, 104, 108), (108, 109, 107, 108)]
    client, market, db, notes, bot = make_testnet_bot(cfg, rows)
    pos = bot.handle_signal(signal(), T0 + 30_000, 1000.0)
    assert pos.status == "open" and pos.mode == "testnet" and pos.protection.get("tp_order_id")  # the OCO pair
    replaces = lambda: [c for c in client.ex.calls if c[0] == "replace"]  # noqa: E731
    client.ex.set_price("BTC/USDT", 108.0)
    market.price, market.now = 108.0, T0 + 4 * MIN
    bot.manage_positions(T0 + 4 * MIN + 1)  # replays the bars: +1R reached, then the trail behind 109
    pos = db.get_position(pos.id)
    assert pos.breakeven_moved and pos.stop_loss == pytest.approx(106.0)
    assert len(replaces()) == 1 and replaces()[0][2] == "ok"  # one request took the pair to the trailing level
    o = client.ex.orders[pos.sl_order_id]
    assert o["triggerPrice"] == pytest.approx(106.0) and o["_list"] == -1 and pos.protection["moved"]
    assert all(cov["BTC/USDT"] >= pos.amount - 1e-9 for op, cov in client.ex.coverage_log
               if op in ("replace", "cancel"))  # never without a stop
    client.ex.set_price("BTC/USDT", 105.5)  # the exchange stop fires: the bot did nothing for it
    bot.manage_positions(T0 + 5 * MIN)
    [closed] = db.closed_positions("testnet")
    assert closed.exit_reason == "trailing_stop" and closed.exit_price == pytest.approx(105.5)
    assert client.ex.free["BTC"] == pytest.approx(0.0) and any("SELL" in m or "Trailing" in m or "trailing" in m
                                                                for m in notes.messages)


def test_the_take_profit_on_the_exchange_closes_a_trade_that_never_moved(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)])
    pos = bot.handle_signal(signal(tp=108.0, trail=None), T0 + 30_000, 1000.0)  # 1.6R target
    client.ex.set_price("BTC/USDT", 108.5)  # straight to the target while the bot looks away
    bot.manage_positions(T0 + 2 * MIN)
    [closed] = db.closed_positions("testnet")
    assert closed.id == pos.id and closed.exit_reason == "take_profit" and closed.exit_price == pytest.approx(108.5)


def test_a_move_the_bot_died_during_is_settled_at_start_up(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)])
    pos = bot.handle_signal(signal(), T0 + 30_000, 1000.0)
    client.ex.set_price("BTC/USDT", 106.0)
    # the move reached the exchange and the process died before recording the answer
    pos = db.get_position(pos.id)
    pos.stop_loss, pos.breakeven_moved = pos.entry_price, True
    move = {"from": pos.sl_order_id, "from_tp": pos.protection["tp_order_id"], "cid": "tbsCRASHED",
            "level": pos.entry_price, "sent_ms": bot.broker.clock()}
    pos.protection = {**pos.protection, "move": move}
    db.update_position(pos)
    mid = client.ex.market_id("BTC/USDT")
    client.ex.privatePostOrderCancelReplace({"symbol": mid, "side": "SELL", "type": "STOP_LOSS",
                                             "quantity": client.ex.amount_to_precision("BTC/USDT", pos.amount),
                                             "stopPrice": client.ex.price_to_precision("BTC/USDT", pos.entry_price),
                                             "cancelReplaceMode": "STOP_ON_FAILURE", "cancelOrderId": move["from"],
                                             "newClientOrderId": "tbsCRASHED"})
    restarted = TradingBot(cfg, market, bot.broker, db, MemoryNotifier(), selection=selection())
    restarted.check_unresolved()
    pos = db.get_position(pos.id)
    assert "move" not in pos.protection and client.ex.orders[pos.sl_order_id]["clientOrderId"] == "tbsCRASHED"
    assert [o["status"] for o in client.ex.orders.values() if o["status"] == "open"] == ["open"]  # nothing stray


def test_a_move_that_never_reached_the_exchange_leaves_the_pair_in_place(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)])
    pos = bot.handle_signal(signal(), T0 + 30_000, 1000.0)
    stop_id = pos.sl_order_id
    pos = db.get_position(pos.id)
    pos.protection = {**pos.protection, "move": {"from": stop_id, "from_tp": pos.protection["tp_order_id"],
                                                 "cid": "tbsNEVERSENT", "level": 100.0, "sent_ms": bot.broker.clock()}}
    db.update_position(pos)
    TradingBot(cfg, market, bot.broker, db, MemoryNotifier(), selection=selection()).check_unresolved()
    pos = db.get_position(pos.id)
    assert "move" not in pos.protection and pos.sl_order_id == stop_id and client.ex.orders[stop_id]["status"] == "open"


def test_the_stray_order_sweep_keeps_the_take_profit_leg(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)])
    pos = bot.handle_signal(signal(), T0 + 30_000, 1000.0)
    bot._sweep_orphans(T0 + MIN, force=True)
    assert client.ex.orders[pos.protection["tp_order_id"]]["status"] == "open"


def test_the_core_trades_on_the_exchange_in_testnet_mode(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)], core=0.65)
    assert bot.core is not None and bot.core.executor is bot.broker
    assert bot.broker.reserved == bot.core.reserved
    bot.core.initialize(650.0)
    assert bot.broker.available_cash() == pytest.approx(10_000.0 - 650.0)


def test_paper_keeps_simulating_the_core(cfg):
    from tradebot.backtest.engine import Costs
    from tradebot.execution import PaperBroker

    cfg.core.fraction = 0.65
    db = Database(cfg.state_path / "p.db")
    bot = TradingBot(cfg, StubMarket([(100, 101, 99, 100)], T0, 100.0), PaperBroker(db, Costs(0.001, 0.0005), 1000.0),
                     db, MemoryNotifier(), selection=selection())
    assert bot.core is not None and bot.core.executor is None


def test_a_position_left_by_an_older_bot_gets_the_pair_on_start_up(cfg):
    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)])
    client.ex.create_order("BTC/USDT", "market", "buy", 1.0, None, {"clientOrderId": "tbeOLD"})
    sig = signal()
    old = Position.from_signal(sig, "testnet", 0.999, T0)
    old.status, old.entry_price = "open", 100.0
    db.insert_position(old)
    bot.check_unresolved()
    p = db.get_position(old.id)
    assert p.sl_order_id and p.protection.get("tp_order_id") and client.ex.coverage() == pytest.approx(0.999)


def test_an_unresolved_core_order_holds_the_breakers_and_new_entries(cfg):
    import ccxt

    client, market, db, notes, bot = make_testnet_bot(cfg, [(100, 101, 99, 100)], core=0.65)
    bot.core.initialize(650.0)
    client.ex.fail["buy"] = ("after", ccxt.RequestTimeout("timed out"))
    client.ex.down = 5  # and the lookup can't run yet
    with pytest.raises(Exception):
        bot.core.rebalance(T0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    assert bot.core.pending
    assert bot._entry_block(signal(), [], 1000.0) == "a core order is unresolved"
    client.ex.down = 5  # still unreachable at the next tick: the resolution fails again
    bot.tick(T0 + 30_000)
    assert bot.core.pending
    assert db.kv_get("testnet:peak_equity") is None  # no breaker read the half-booked equity
    bot.tick(T0 + 60_000)  # the order resolves: everything resumes
    assert not bot.core.pending and bot._entry_block(signal(), [], 1000.0) != "a core order is unresolved"
