"""Exchange-side exits on the testnet path: the OCO pair at entry, stop moves in one cancel-replace
request, every way a move can fail, closing, and the core sleeve's orders. On a fake exchange with
Binance spot semantics (tests/fakespot.py)."""
import ccxt
import pytest

from tradebot.backtest.engine import Costs
from tradebot.config import CoreConfig
from tradebot.core import CoreSleeve
from tradebot.db import Database
from tradebot.execution import LiveBroker
from tradebot.models import Position, Signal

from .fakespot import FakeSpotClient
from .test_brokers import Clock


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr("tradebot.data.exchange.time.sleep", lambda s: None)


def broker(prices=None, **kw):
    client, clock = FakeSpotClient(prices), Clock()
    saved = []

    def persist(p):  # what was saved, and how many requests the exchange had seen by then
        saved.append((len(client.ex.calls), p.sl_client_id, dict(p.protection or {})))

    b = LiveBroker(client, "USDT", sleep=clock.sleep, clock=clock, fill_timeout_s=2, persist=persist, testnet=True, **kw)
    return client, b, saved


def position(b, entry=100.0, stop=95.0, tp=110.0, amount=1.0):
    sig = Signal("BTC/USDT", "1h", "trend", "long", entry, stop, tp, 0, 0, 0, 10)
    p = Position.from_signal(sig, "testnet", amount, 0)
    p.id = 7
    b.open_position(p, entry, 1)
    return p


def protected(**kw):
    client, b, saved = broker(**kw)
    p = position(b)
    b.protect(p, 2)
    return client, b, saved, p


def calls(client, kind):
    return [c for c in client.ex.calls if c[0] == kind]


# ---------------------------------------------------------------- the pair at entry
def test_entry_is_protected_by_an_oco_pair_checked_on_the_exchange():
    client, b, saved, p = protected()
    ex = client.ex
    stop_leg, tp_leg = ex.orders[p.sl_order_id], ex.orders[p.protection["tp_order_id"]]
    assert (stop_leg["_btype"], stop_leg["triggerPrice"], stop_leg["status"]) == ("STOP_LOSS", 95.0, "open")
    assert (tp_leg["_btype"], tp_leg["triggerPrice"], tp_leg["status"]) == ("TAKE_PROFIT", 110.0, "open")
    assert stop_leg["_list"] == tp_leg["_list"] == p.protection["list_id"]
    assert stop_leg["amount"] == pytest.approx(p.amount) and ex.locked["BTC"] == pytest.approx(p.amount)
    assert p.protection["stop"] == 95.0 and p.protection["tp"] == 110.0 and not p.protection.get("moved")
    assert ex.coverage() == pytest.approx(p.amount)


def test_the_pair_s_client_ids_are_saved_before_it_is_sent():
    client, b, saved, p = protected()
    sent_at = next(i for i, c in enumerate(client.ex.calls) if c[0] == "oco")
    oco = client.ex.calls[sent_at][1]
    before = [s for n, cid, prot in saved if n <= sent_at for s in [(cid, prot)]]
    assert any(cid == oco["belowClientOrderId"] and prot.get("tp_client_id") == oco["aboveClientOrderId"]
               and prot.get("list_client_id") == oco["listClientOrderId"] for cid, prot in before)


def test_a_refused_pair_falls_back_to_a_single_stop():
    client, b, saved = broker()
    p = position(b)
    p.take_profit = 99.0  # below the market: Binance refuses the pair's price relationship
    b.protect(p, 2)
    o = client.ex.orders[p.sl_order_id]
    assert o["_btype"] == "STOP_LOSS" and o["_list"] == -1 and "tp_order_id" not in p.protection
    assert client.ex.coverage() == pytest.approx(p.amount)


def test_a_pair_whose_answer_was_lost_is_found_by_its_client_ids():
    client, b, saved = broker()
    p = position(b)
    client.ex.fail["oco"] = ("after", ccxt.RequestTimeout("timed out"))
    b.protect(p, 2)
    assert client.ex.orders[p.protection["tp_order_id"]]["_btype"] == "TAKE_PROFIT"
    assert len(calls(client, "oco")) == 1 and not calls(client, "create")[1:]  # no second pair, no extra stop


def test_a_pair_that_never_arrived_falls_back_to_a_single_stop():
    client, b, saved = broker()
    p = position(b)
    client.ex.fail["oco"] = ("before", ccxt.RequestTimeout("timed out"))
    b.protect(p, 2)
    assert client.ex.orders[p.sl_order_id]["_list"] == -1 and client.ex.coverage() == pytest.approx(p.amount)


def test_without_take_profit_orders_the_stop_stands_alone():
    client, b, saved = broker(take_profit_orders=False)
    p = position(b)
    b.protect(p, 2)
    assert not calls(client, "oco") and client.ex.orders[p.sl_order_id]["_list"] == -1


# ---------------------------------------------------------------- exits on the exchange
def test_the_take_profit_leg_fills_while_the_bot_is_away():
    client, b, saved, p = protected()
    stop_id = p.sl_order_id
    client.ex.set_price("BTC/USDT", 111.0)
    assert b.sync([p], 10) == []
    assert p.status == "closed" and p.exit_reason == "take_profit" and p.exit_price == pytest.approx(111.0)
    assert client.ex.orders[stop_id]["status"] == "expired" and client.ex.free["BTC"] == pytest.approx(0.0)


def test_the_stop_leg_fills_before_any_move():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 94.0)
    b.sync([p], 10)
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.0)


def test_a_partly_filled_take_profit_leaves_the_rest_for_the_bot_to_sell():
    client, b, saved, p = protected()
    client.ex.fill_fraction = 0.5
    client.ex.set_price("BTC/USDT", 111.0)
    b.sync([p], 10)
    assert p.status == "open" and p.closing_reason == "take_profit" and p.exit_filled == pytest.approx(p.amount / 2)
    client.ex.fill_fraction = 1.0
    b.close_position(p, 111.0, p.closing_reason, 11)
    assert p.status == "closed" and p.exit_reason == "take_profit" and client.ex.free["BTC"] == pytest.approx(0.0)


# ---------------------------------------------------------------- moving the stop
def breakeven(client, b, p, price=106.0):
    client.ex.set_price("BTC/USDT", price)
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)


def test_the_breakeven_move_is_one_request_and_never_leaves_the_coins_without_a_stop():
    client, b, saved, p = protected()
    stop_id, tp_id, before = p.sl_order_id, p.protection["tp_order_id"], len(client.ex.calls)
    breakeven(client, b, p)
    after = client.ex.calls[before:]
    assert [c[0] for c in after] == ["replace"] and after[0][2] == "ok"  # one request: no cancel, then create
    assert after[0][1]["cancelOrderId"] == stop_id and after[0][1]["cancelReplaceMode"] == "STOP_ON_FAILURE"
    protected_from = next(i for i, (op, _) in enumerate(client.ex.coverage_log) if op == "oco")
    assert all(cov["BTC/USDT"] >= p.amount - 1e-9 for _, cov in client.ex.coverage_log[protected_from:])
    ex = client.ex
    assert ex.orders[stop_id]["status"] == ex.orders[tp_id]["status"] == "canceled"  # the pair went together
    new = ex.orders[p.sl_order_id]
    assert (new["_btype"], new["triggerPrice"], new["_list"], new["status"]) == ("STOP_LOSS", p.entry_price, -1, "open")
    assert p.protection["moved"] and p.protection["stop"] == pytest.approx(p.entry_price) and "tp_order_id" not in p.protection
    assert "move" not in p.protection and ex.coverage() == pytest.approx(p.amount)


def test_the_move_is_saved_before_its_request_is_sent():
    client, b, saved, p = protected()
    breakeven(client, b, p)
    sent_at = next(i for i, c in enumerate(client.ex.calls) if c[0] == "replace")
    req = client.ex.calls[sent_at][1]
    assert any(n <= sent_at and prot.get("move", {}).get("cid") == req["newClientOrderId"] for n, _, prot in saved)


def test_trailing_moves_in_steps_and_never_down():
    client, b, saved, p = protected()
    breakeven(client, b, p, price=109.0)  # under the take-profit
    n = len(client.ex.calls)
    per_unit = p.entry_price - p.initial_stop
    b.move_stop(p, p.entry_price + 0.05 * per_unit)  # half a step: waits
    b.sync_stop(p, 21)
    assert len(client.ex.calls) == n
    b.move_stop(p, p.entry_price + 0.3 * per_unit)
    b.sync_stop(p, 22)
    assert [c[0] for c in client.ex.calls[n:]] == ["replace"]
    assert client.ex.orders[p.sl_order_id]["triggerPrice"] == pytest.approx(round(p.entry_price + 0.3 * per_unit, 2))
    p.stop_loss = p.entry_price  # the bot never loosens it; even if it did, the exchange stop stays
    b.sync_stop(p, 23)
    assert [c[0] for c in client.ex.calls[n:]] == ["replace"]


def test_breakeven_and_trailing_stops_are_named_by_their_level():
    client, b, saved, p = protected()
    breakeven(client, b, p)
    client.ex.set_price("BTC/USDT", 99.0)
    b.sync([p], 30)
    assert p.status == "closed" and p.exit_reason == "breakeven_stop"
    client, b, saved, p = protected()
    breakeven(client, b, p, price=109.0)
    b.move_stop(p, 104.0)
    b.sync_stop(p, 31)
    client.ex.set_price("BTC/USDT", 103.0)
    b.sync([p], 32)
    assert p.status == "closed" and p.exit_reason == "trailing_stop" and p.exit_price == pytest.approx(103.0)


def test_a_move_the_price_is_already_through_sells_now():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 99.0)  # below the new level, above the old stop
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    assert p.closing_reason == "breakeven_stop" and p.sl_order_id is None
    b.close_position(p, 99.0, p.closing_reason, 21)
    assert p.status == "closed" and p.exit_reason == "breakeven_stop" and len(calls(client, "create")) == 2  # buy, sell


def test_a_move_after_the_stop_already_executed_leaves_it_to_sync():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 94.0)  # the stop leg sold before the move reached the exchange
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    assert [c[2] for c in calls(client, "replace")] == ["cancel failed"] and "move" not in p.protection
    b.sync([p], 21)
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.0)


def test_a_refused_replacement_is_followed_by_a_new_stop_at_once():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace_new"] = True  # the cancel succeeds, the new stop is refused
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    assert [c[0] for c in client.ex.calls[-2:]] == ["replace", "create"]
    o = client.ex.orders[p.sl_order_id]
    assert o["_btype"] == "STOP_LOSS" and o["triggerPrice"] == pytest.approx(p.entry_price) and o["status"] == "open"
    assert not p.closing_reason and p.protection["moved"] and client.ex.coverage() == pytest.approx(p.amount)


def test_a_move_whose_answer_was_lost_is_adopted_by_its_client_id():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace"] = ("after", ccxt.RequestTimeout("timed out"))
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    assert client.ex.orders[p.sl_order_id]["triggerPrice"] == pytest.approx(p.entry_price)
    assert p.protection["moved"] and "move" not in p.protection and len(calls(client, "replace")) == 1


def test_a_move_lost_before_the_exchange_keeps_the_pair_and_is_tried_again():
    client, b, saved, p = protected()
    stop_id = p.sl_order_id
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace"] = ("before", ccxt.RequestTimeout("timed out"))
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    assert p.sl_order_id == stop_id and client.ex.orders[stop_id]["status"] == "open" and "move" not in p.protection
    b.sync_stop(p, 21)
    assert client.ex.orders[p.sl_order_id]["triggerPrice"] == pytest.approx(p.entry_price)


def test_a_move_in_flight_is_settled_before_sync_looks_at_the_orders():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace"] = ("after", ccxt.RequestTimeout("timed out"))
    client.ex.down = 5  # the exchange can't be asked for a while (every retry fails): the resolution waits
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    with pytest.raises(ccxt.ExchangeNotAvailable):
        b.sync_stop(p, 20)
    assert p.protection["move"] and b.sync([p], 21) == []  # sync leaves it alone
    b.sync_stop(p, 22)  # next check: resolved by the client id
    assert "move" not in p.protection and client.ex.orders[p.sl_order_id]["triggerPrice"] == pytest.approx(p.entry_price)


# ---------------------------------------------------------------- closing
def test_closing_cancels_the_pair_and_sells_once():
    client, b, saved, p = protected()
    stop_id, tp_id = p.sl_order_id, p.protection["tp_order_id"]
    client.ex.set_price("BTC/USDT", 105.0)
    b.close_position(p, 105.0, "time_stop", 30)
    assert p.status == "closed" and p.exit_reason == "time_stop"
    assert client.ex.orders[stop_id]["status"] == client.ex.orders[tp_id]["status"] == "canceled"
    assert len([c for c in calls(client, "create") if c[1] == "sell"]) == 1 and client.ex.free["BTC"] == pytest.approx(0)


def test_a_take_profit_filling_while_closing_is_not_sold_twice(monkeypatch):
    client, b, saved, p = protected()
    real = client.ex.cancel_order

    def cancel_after_the_trigger(oid, symbol=None, params=None):
        client.ex.set_price("BTC/USDT", 111.0)  # the take-profit triggers as the bot cancels
        return real(oid, symbol, params)

    monkeypatch.setattr(client.ex, "cancel_order", cancel_after_the_trigger)
    b.close_position(p, 108.0, "exit_signal", 30)
    assert p.status == "closed" and p.exit_reason == "take_profit"
    assert not [c for c in calls(client, "create") if c[1] == "sell"]


# ---------------------------------------------------------------- the testnet guard
def test_the_testnet_broker_refuses_a_client_on_the_real_exchange():
    client = FakeSpotClient(sandbox=False)
    with pytest.raises(RuntimeError, match="not on the testnet"):
        LiveBroker(client, "USDT", testnet=True)
    ok = LiveBroker(FakeSpotClient(), "USDT", testnet=True)
    assert ok.mode == "testnet"


# ---------------------------------------------------------------- the core sleeve's orders
def core_on_exchange(tmp_path, prices=None):
    client, b, saved = broker(prices or {"BTC/USDT": 100.0, "ETH/USDT": 10.0})
    db = Database(tmp_path / "core.db")
    core = CoreSleeve(db, CoreConfig(), Costs(0.001, 0.0005), "testnet", client, executor=b)
    b.reserved = core.reserved
    return client, b, db, core


def test_the_core_trades_with_market_orders_and_books_their_fills(tmp_path):
    client, b, db, core = core_on_exchange(tmp_path)
    core.initialize(1000.0)
    trades = core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.5})
    assert sorted((t.symbol, t.side) for t in trades) == [("BTC/USDT", "buy"), ("ETH/USDT", "buy")]
    buys = [c for c in calls(client, "create") if c[1] == "buy"]
    assert len(buys) == 2 and all(c[2]["clientOrderId"].startswith("tbc") for c in buys)
    h = core.holdings
    assert h["BTC/USDT"] == pytest.approx(client.ex.free["BTC"]) and h["ETH/USDT"] == pytest.approx(client.ex.free["ETH"])
    spent = 10_000.0 - client.ex.free["USDT"]
    assert core.cash == pytest.approx(1000.0 - spent)  # the ledger is what the exchange did
    assert core.pending is None and len(db.core_trades("testnet")) == 2
    sold = core.rebalance(1, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 0.0, "ETH/USDT": 0.0})
    assert {t.side for t in sold} == {"sell"} and core.holdings == {}
    assert client.ex.free["BTC"] == pytest.approx(0.0, abs=1e-6)


def test_the_core_order_is_saved_before_it_is_sent_and_a_lost_answer_is_resolved(tmp_path):
    client, b, db, core = core_on_exchange(tmp_path)
    core.initialize(1000.0)
    client.ex.fail["buy"] = ("after", ccxt.RequestTimeout("timed out"))
    trades = core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    assert len(trades) == 1 and core.holdings["BTC/USDT"] == pytest.approx(client.ex.free["BTC"])
    assert len([c for c in calls(client, "create") if c[1] == "buy"]) == 1  # found by its client id, not bought twice
    # a crash after sending: the saved order is resolved on the next start
    core._set("pending", {"client_id": "tbcLOST", "symbol": "ETH/USDT", "side": "buy", "qty": 1.0,
                          "sent_ms": b.clock(), "reason": "test"})
    client.ex.create_order("ETH/USDT", "market", "buy", 1.0, None, {"clientOrderId": "tbcLOST"})
    t = core.resolve_pending(5)
    assert t.symbol == "ETH/USDT" and core.pending is None and core.holdings["ETH/USDT"] == pytest.approx(0.999)
    core._set("pending", {"client_id": "tbcNEVER", "symbol": "ETH/USDT", "side": "buy", "qty": 1.0,
                          "sent_ms": b.clock(), "reason": "test"})
    assert core.resolve_pending(6) is None and core.pending is None  # never reached the exchange


def test_the_satellite_leaves_the_core_s_cash_and_coins_alone(tmp_path):
    client, b, db, core = core_on_exchange(tmp_path)
    core.initialize(1000.0)
    core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    core_btc = core.holdings["BTC/USDT"]
    p = position(b)
    b.protect(p, 2)
    usdt = client.ex.free["USDT"]
    assert b.available_cash() == pytest.approx(usdt - core.cash)
    assert b.equity({"BTC/USDT": 100.0}, [p]) == pytest.approx(usdt - core.cash + p.amount * 100.0)
    assert b.sync([p], 3) == []  # the core's coins don't hide or fake the satellite's
    client.ex.cancel_order(p.sl_order_id)  # the satellite's coins are freed...
    client.ex.free["BTC"] -= p.amount  # ...and taken outside the bot
    with pytest.raises(Exception, match="sold outside the bot"):
        b.close_position(p, 100.0, "exit_signal", 4)
    assert client.ex.free["BTC"] == pytest.approx(core_btc)  # the core's coins were not sold for it


# ---------------------------------------------------------------- review fixes
def test_an_execution_status_unknown_answer_is_resolved_not_taken_as_a_refusal(tmp_path):
    """-1006 ('execution status unknown') comes back from ccxt as OperationFailed, not NetworkError:
    the order may have filled, so it is looked up by its client id."""
    client, b, db, core = core_on_exchange(tmp_path)
    core.initialize(1000.0)
    client.ex.fail["buy"] = ("after", ccxt.OperationFailed("binance {\"code\":-1006,\"msg\":\"Execution status unknown.\"}"))
    trades = core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    assert len(trades) == 1 and core.holdings["BTC/USDT"] == pytest.approx(client.ex.free["BTC"])
    client2, b2, saved = broker()
    client2.ex.fail["buy"] = ("after", ccxt.OperationFailed("binance status unknown"))
    p = position(b2)  # the entry too: found by its client id, not "nothing was bought"
    assert p.status == "open" and p.amount == pytest.approx(0.999)


def test_a_filter_failure_in_a_cancel_replace_answer_is_read_from_its_body():
    """ccxt raises a -2021 whose new order failed PRICE_FILTER as InvalidOrder (its body scan comes
    first): the body still says the cancel happened, so a stop goes back at once, with no wait."""
    import json

    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    real = client.ex.privatePostOrderCancelReplace

    def filter_failure(params):
        old = client.ex.orders[str(params["cancelOrderId"])]
        client.ex._cancel_order(old)
        raise ccxt.InvalidOrder("binance " + json.dumps({
            "code": -2021, "msg": "Order cancel-replace partially failed.",
            "data": {"cancelResult": "SUCCESS", "newOrderResult": "FAILURE",
                     "cancelResponse": {"orderId": int(old["id"]), "status": "CANCELED"},
                     "newOrderResponse": {"code": -1013, "msg": "Filter failure: PRICE_FILTER"}}}))

    client.ex.privatePostOrderCancelReplace = filter_failure
    slept = []
    b.sleep = lambda s: slept.append(s)
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    b.sync_stop(p, 20)
    client.ex.privatePostOrderCancelReplace = real
    assert not slept  # no recvWindow wait with the coins unprotected
    assert client.ex.coverage() == pytest.approx(p.amount) and client.ex.orders[p.sl_order_id]["status"] == "open"


def test_the_sweep_keeps_a_stop_known_only_by_an_unresolved_move_s_client_id():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace"] = ("after", ccxt.RequestTimeout("timed out"))
    client.ex.down = 5  # the move can't be resolved yet
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    with pytest.raises(ccxt.ExchangeNotAvailable):
        b.sync_stop(p, 20)
    new_cid = p.protection["move"]["cid"]
    known = {p.sl_order_id, p.protection.get("tp_order_id"), new_cid}
    assert b.cancel_orphans({"BTC/USDT"}, {k for k in known if k}) == []
    assert client.ex.orders[client.ex.by_cid[new_cid]]["status"] == "open"


def test_an_order_read_back_takes_its_fees_from_its_trades(tmp_path):
    """Binance's order query has no fees: a buy found by lookup is booked with the coins the fee
    took, from the order's trades - the ledger and the account agree."""
    client, b, db, core = core_on_exchange(tmp_path)
    core.initialize(1000.0)
    client.ex.fail["buy"] = ("after", ccxt.RequestTimeout("timed out"))
    core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    assert core.holdings["BTC/USDT"] == pytest.approx(client.ex.free["BTC"])  # the fee's coins are not booked
    assert any(c[0] == "trades" for c in client.ex.calls)
    sold = core.rebalance(1, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 0.0, "ETH/USDT": 0.0})
    assert [t.side for t in sold] == ["sell"] and core.holdings == {}  # its exit is not refused for missing coins


def test_a_bnb_fee_is_not_charged_to_the_core_s_cash(tmp_path):
    client, b, db, core = core_on_exchange(tmp_path, {"BTC/USDT": 100.0, "ETH/USDT": 10.0, "BNB/USDT": 500.0})
    client.ex.bnb_fees = True
    client.ex.free["BNB"] = 1.0
    core.initialize(1000.0)
    [t] = core.rebalance(0, {"BTC/USDT": 100.0, "ETH/USDT": 10.0}, {"BTC/USDT": 1.0, "ETH/USDT": 0.0})
    spent = 10_000.0 - client.ex.free["USDT"]
    assert core.cash == pytest.approx(1000.0 - spent)  # cash moved as the USDT balance did
    assert t.fee > 0 and core.holdings["BTC/USDT"] == pytest.approx(client.ex.free["BTC"])  # the fee is recorded


def test_a_protection_that_sold_during_its_move_closes_the_position_once():
    client, b, saved, p = protected()
    client.ex.set_price("BTC/USDT", 106.0)
    client.ex.fail["replace"] = ("before", ccxt.RequestTimeout("timed out"))  # the move is in doubt...
    b.move_stop(p, p.entry_price)
    p.breakeven_moved = True
    real = b._resolve_move

    def take_profit_fires_first(pos, now_ms):  # ...and the take-profit fires before it is resolved
        client.ex.set_price("BTC/USDT", 111.0)
        return real(pos, now_ms)

    b._resolve_move = take_profit_fires_first
    b.sync_stop(p, 20)
    assert p.status == "closed" and p.exit_reason == "take_profit" and p.exit_filled == pytest.approx(p.amount)
    assert b.sync([p], 21) == []  # nothing counted twice, nothing left


def test_a_leg_that_fires_while_the_pair_is_read_back_is_booked_from_the_fresh_read():
    client, b, saved = broker()
    p = position(b)
    real = b._verify_stop
    fired = []

    def stop_fires_on_read_back(order, symbol, stop, qty):
        if not fired:
            fired.append(1)
            client.ex.set_price("BTC/USDT", 94.0)  # the stop leg triggers between placing and checking
        return real(order, symbol, stop, qty)

    b._verify_stop = stop_fires_on_read_back
    b.protect(p, 2)
    assert p.status == "closed" and p.exit_filled == pytest.approx(p.amount) and p.exit_price == pytest.approx(94.0)
