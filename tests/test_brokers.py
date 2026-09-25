import ccxt
import pytest

from tradebot.backtest.engine import Costs
from tradebot.db import Database
from tradebot.execution import LiveBroker, PaperBroker
from tradebot.execution.base import ExecutionError, NotFilled, ProtectionError
from tradebot.execution.live import VENUES
from tradebot.models import Position, Signal


def sig(side="long"):
    if side == "long":
        return Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, 110.0, 0, 0, 0, 10)
    return Signal("BTC/USDT", "1h", "trend", "short", 100.0, 105.0, 90.0, 0, 0, 0, 10)


def pending(side="long", amount=1.0, mode="paper"):
    p = Position.from_signal(sig(side), mode, amount, 0)
    p.id = 7
    return p


# ------------------------------------------------------------------ paper
def test_paper_long_round_trip(tmp_path):
    db = Database(tmp_path / "t.db")
    b = PaperBroker(db, Costs(0.001, 0.0005), 1000)
    p = b.open_position(pending(amount=2.0), 100.0, 1)
    assert p.status == "open" and p.entry_price == pytest.approx(100.05)
    assert b.cash == pytest.approx(1000 - 100.05 * 2 * 0.001)
    assert b.equity({"BTC/USDT": 105.0}, [p]) == pytest.approx(b.cash + (105 - 100.05) * 2)
    p = b.close_position(p, 110.0, "take_profit", 2)
    exit_fill = 110 * (1 - 0.0005)  # market exit pays slippage, like live
    assert p.exit_price == pytest.approx(exit_fill)
    expected_pnl = (exit_fill - 100.05) * 2 - 100.05 * 2 * 0.001 - exit_fill * 2 * 0.001
    assert p.pnl == pytest.approx(expected_pnl)
    assert b.cash == pytest.approx(1000 + expected_pnl)
    assert p.r_multiple == pytest.approx(expected_pnl / ((100.05 - 95) * 2))


def test_paper_short_stop(tmp_path):
    db = Database(tmp_path / "t.db")
    b = PaperBroker(db, Costs(0.0, 0.0), 1000)
    p = b.open_position(pending("short"), 100.0, 1)
    p = b.close_position(p, 105.0, "stop_loss", 2)
    assert p.pnl == pytest.approx(-5.0) and p.r_multiple == pytest.approx(-1.0)
    assert b.cash == pytest.approx(995.0)


# ------------------------------------------------------------ fake venue
class FakeExchange:
    """Minimal spot exchange: balances, locked coins, trigger orders routed like the real venue."""

    def __init__(self, venue="binance", price=100.0):
        self.venue = venue
        self.price = price
        self.orders = {}
        self.calls = []  # (method, id/type, params)
        self.free = {"USDT": 10_000.0, "BTC": 0.0}
        self.locked = {"BTC": 0.0}
        self.stop_mode = "normal"  # normal | ignore (plain sell) | no_trigger | reject_market | reject_all
        self.sell_fill_fraction = 1.0
        self.buy_fill_fraction = 1.0
        self.fail_next = {}  # method -> exception to raise once
        self.on_cancel_stop = None  # hook(order) -> may mutate before cancel

    # --- helpers
    def _maybe_fail(self, method):
        exc = self.fail_next.pop(method, None)
        if exc:
            raise exc

    def _is_stop_route(self, params):
        if self.venue == "okx":
            return bool(params.get("trigger"))
        if self.venue == "bybit":
            return params.get("orderFilter") == "tpslOrder"
        return None  # one route for everything

    def _lookup(self, oid, params):
        o = self.orders.get(oid)
        route = self._is_stop_route(params)
        if o is None or (route is not None and route != o["_stop"]):
            raise ccxt.OrderNotFound(oid)
        return o

    def _fill_sell(self, amount, price):
        self.free["BTC"] -= amount
        self.free["USDT"] += amount * price * (1 - 0.001)

    # --- ccxt surface
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        self.calls.append(("create", f"{type}/{side}", dict(params)))
        self._maybe_fail("create_stop" if "stopLossPrice" in params else f"create_{side}")
        oid = str(len(self.orders) + 1)
        if "stopLossPrice" in params:
            if self.stop_mode == "reject_all" or (self.stop_mode == "reject_market" and type == "market"):
                raise ccxt.InvalidOrder("stop type not supported")
            if self.stop_mode == "ignore":  # venue ignored the trigger: plain immediate sell
                self._fill_sell(amount, self.price)
                o = {"id": oid, "status": "closed", "side": "sell", "amount": amount, "filled": amount,
                     "average": self.price, "_stop": False}
            else:
                self.free["BTC"] -= amount
                self.locked["BTC"] += amount
                trig = None if self.stop_mode == "no_trigger" else params["stopLossPrice"]
                o = {"id": oid, "status": "open", "side": "sell", "type": type, "amount": amount, "filled": 0.0,
                     "triggerPrice": trig, "_stop": self.stop_mode != "no_trigger", "_locked": True}
        elif side == "buy":
            filled = amount * self.buy_fill_fraction
            fee = filled * 0.001
            self.free["BTC"] += filled - fee
            self.free["USDT"] -= filled * self.price
            status = "closed" if filled >= amount else "open"
            o = {"id": oid, "status": status, "side": "buy", "amount": amount, "filled": filled,
                 "average": self.price, "fee": {"currency": "BTC", "cost": fee}, "_stop": False}
        else:
            if amount > self.free["BTC"] + 1e-12:
                raise ccxt.InsufficientFunds("not enough BTC")
            filled = amount * self.sell_fill_fraction
            self._fill_sell(filled, self.price)
            status = "closed" if filled >= amount else "open"
            o = {"id": oid, "status": status, "side": "sell", "amount": amount, "filled": filled,
                 "average": self.price, "fee": {"currency": "USDT", "cost": filled * self.price * 0.001},
                 "_stop": False}
        self.orders[oid] = o
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def fetch_order(self, oid, symbol=None, params=None):
        params = params or {}
        self.calls.append(("fetch", oid, dict(params)))
        self._maybe_fail("fetch")
        if self.venue == "bybit" and not params.get("acknowledged"):
            raise ccxt.ArgumentsRequired("set params['acknowledged'] = True")
        o = self._lookup(oid, params)
        return {k: v for k, v in o.items() if not k.startswith("_")}

    def cancel_order(self, oid, symbol=None, params=None):
        params = params or {}
        self.calls.append(("cancel", oid, dict(params)))
        o = self._lookup(oid, params)
        if o["_stop"] and self.on_cancel_stop:
            self.on_cancel_stop(o)
        if o["status"] != "open":
            raise ccxt.OrderNotFound(oid)
        if o.get("_locked"):
            self.free["BTC"] += o["amount"]
            self.locked["BTC"] -= o["amount"]
        o["status"] = "canceled"

    def trigger_stop(self, oid, price):
        o = self.orders[oid]
        self.locked["BTC"] -= o["amount"]
        self.free["USDT"] += o["amount"] * price * (1 - 0.001)
        o.update(status="closed", filled=o["amount"], average=price)

    def fetch_balance(self):
        total = {k: self.free.get(k, 0.0) + self.locked.get(k, 0.0) for k in set(self.free) | set(self.locked)}
        return {"free": dict(self.free), "total": total}


class FakeClient:
    def __init__(self, venue="binance", price=100.0):
        self.id = venue
        self.ex = FakeExchange(venue, price)

    def market(self, symbol):
        return {"base": "BTC", "quote": "USDT"}

    def limits(self, symbol):
        return {"min_amount": 0.0001, "min_cost": 5.0}

    def amount_to_precision(self, symbol, a):
        return float(int(a * 1e6) / 1e6)

    def price_to_precision(self, symbol, p):
        return float(f"{p:.2f}")

    def fetch_last_price(self, symbol):
        return self.ex.price


def live(venue="binance", **kw):
    c = FakeClient(venue)
    return c, LiveBroker(c, "USDT", sleep=lambda s: None, fill_timeout_s=kw.pop("fill_timeout_s", 2), **kw)


def opened(venue="binance", amount=1.0):
    c, b = live(venue)
    p = b.open_position(pending(amount=amount, mode="live"), 100.0, 1)
    return c, b, p


# ------------------------------------------------------------------ entry
def test_entry_uses_client_id_and_counts_coin_fee():
    c, b, p = opened()
    kind, params = c.ex.calls[0][1], c.ex.calls[0][2]
    assert kind == "market/buy" and params["clientOrderId"] == p.client_order_id
    assert p.client_order_id.isalnum() and len(p.client_order_id) <= 18
    assert p.status == "open" and p.amount == pytest.approx(0.999)
    assert p.fees == pytest.approx(0.1)  # 0.001 BTC fee valued at 100


def test_base_fee_in_realized_pnl():
    """Review #10: buy 1 BTC @100 (0.001 BTC fee), sell 0.999 @108."""
    c, b, p = opened()
    c.ex.price = 108.0
    p = b.close_position(p, 108.0, "take_profit", 2)
    sell_fee = 0.999 * 108 * 0.001
    assert p.pnl == pytest.approx(0.999 * 108 - 100.0 - sell_fee)  # 7.892 - exit fee


def test_entry_rejected_is_not_filled():
    c, b = live()
    c.ex.fail_next["create_buy"] = ccxt.InsufficientFunds("no money")
    with pytest.raises(NotFilled):
        b.open_position(pending(mode="live"), 100.0, 1)


def test_ambiguous_entry_adopts_coins_that_arrived():
    c, b = live()

    class Timeout(ccxt.RequestTimeout):
        pass

    real_create = c.ex.create_order

    def create_then_timeout(*a, **k):
        real_create(*a, **k)  # the exchange executes it...
        raise Timeout("read timed out")  # ...but the response is lost

    c.ex.create_order = create_then_timeout
    p = b.open_position(pending(mode="live"), 100.0, 1)
    assert p.status == "open" and p.amount == pytest.approx(0.999)


def test_ambiguous_entry_with_no_coins_is_unknown_not_forgotten():
    c, b = live()
    c.ex.fail_next["create_buy"] = ccxt.RequestTimeout("timeout")
    with pytest.raises(ExecutionError):
        b.open_position(pending(mode="live"), 100.0, 1)


def test_partial_entry_cancels_remainder_and_sizes_from_fill():
    c, b = live()
    c.ex.buy_fill_fraction = 0.6
    p = b.open_position(pending(mode="live"), 100.0, 1)
    assert p.amount == pytest.approx(0.6 * 0.999)
    assert any(call[0] == "cancel" for call in c.ex.calls)


# ------------------------------------------------------------- protection
def test_protect_places_and_verifies_stop():
    c, b, p = opened()
    b.protect(p, 2)
    o = c.ex.orders[p.sl_order_id]
    assert o["status"] == "open" and o["triggerPrice"] == 95.0 and o["amount"] == pytest.approx(0.999)
    assert c.ex.locked["BTC"] == pytest.approx(0.999)


def test_stop_limit_fallback_then_failure():
    c, b, p = opened()
    c.ex.stop_mode = "reject_market"
    b.protect(p, 2)
    assert c.ex.orders[p.sl_order_id]["type"] == "limit"
    c2, b2, p2 = opened()
    c2.ex.stop_mode = "reject_all"
    with pytest.raises(ProtectionError):  # review #4: absence of protection is an error
        b2.protect(p2, 2)
    assert p2.sl_order_id is None


def test_stop_that_executes_immediately_is_not_protection():
    """Review #6: the venue treated the stop as a plain sell."""
    c, b, p = opened()
    c.ex.stop_mode = "ignore"
    b.protect(p, 2)
    assert p.sl_order_id is None
    assert p.status == "closed" and p.exit_reason == "stop_order_executed"


def test_open_order_without_trigger_is_cancelled():
    c, b, p = opened()
    c.ex.stop_mode = "no_trigger"
    with pytest.raises(ProtectionError):
        b.protect(p, 2)
    assert all(o["status"] != "open" for o in c.ex.orders.values())
    assert c.ex.free["BTC"] == pytest.approx(0.999)  # nothing left locked


def test_stop_network_error_is_unprotected():
    c, b, p = opened()
    c.ex.fail_next["create_stop"] = ccxt.RequestTimeout("timeout")
    with pytest.raises(ProtectionError):
        b.protect(p, 2)


# ----------------------------------------------------------------- routing
@pytest.mark.parametrize("venue", ["okx", "bybit"])
def test_trigger_order_routing_on_close(venue):
    """Review #1: the stop must be cancelled via its own route before the bot sells."""
    c, b, p = opened(venue)
    b.protect(p, 2)
    stop_id = p.sl_order_id
    p = b.close_position(p, 108.0, "take_profit", 3)
    assert c.ex.orders[stop_id]["status"] == "canceled"  # not left active
    assert p.status == "closed" and p.exit_reason == "take_profit"
    stop_calls = [(m, params) for m, oid, params in c.ex.calls if oid == stop_id]
    assert {m for m, _ in stop_calls} == {"fetch", "cancel"}
    for method, params in stop_calls:
        expected = VENUES[venue].stop_fetch if method == "fetch" else VENUES[venue].stop_cancel
        assert all(params.get(k) == v for k, v in expected.items()), (method, params)


def test_bybit_regular_fetch_needs_acknowledged():
    c, b, p = opened("bybit")  # entry settles via fetch_order - would raise without the flag
    assert p.status == "open"


def test_unsupported_venue_refused():
    with pytest.raises(ValueError, match="not supported"):
        LiveBroker(FakeClient("someexchange"), "USDT")


# -------------------------------------------------------------------- exit
def test_close_cancels_stop_then_sells_only_what_is_held():
    c, b, p = opened()
    b.protect(p, 2)
    c.ex.price = 108.0
    p = b.close_position(p, 108.0, "take_profit", 3)
    sells = [call for call in c.ex.calls
             if call[0] == "create" and call[1] == "market/sell" and "stopLossPrice" not in call[2]]
    assert len(sells) == 1
    assert p.exit_price == pytest.approx(108.0) and c.ex.free["BTC"] == pytest.approx(0.0)


def test_stop_filling_during_cancel_never_double_sells():
    """Review #2: fetch says open, the stop fills, cancel then fails with OrderNotFound."""
    c, b, p = opened()
    b.protect(p, 2)
    c.ex.on_cancel_stop = lambda o: c.ex.trigger_stop(o["id"], 94.9)
    p = b.close_position(p, 96.0, "exit_signal", 3)
    sells = [call for call in c.ex.calls
             if call[0] == "create" and call[1] == "market/sell" and "stopLossPrice" not in call[2]]
    assert sells == []
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.9)


def test_partial_exit_keeps_position_open_and_retries():
    """Review #5: 25% sold then the order stalls."""
    c, b, p = opened()
    c.ex.sell_fill_fraction = 0.25
    with pytest.raises(ExecutionError):
        b.close_position(p, 100.0, "time_stop", 3)
    assert p.status == "open" and p.closing_reason == "time_stop"
    assert p.exit_filled == pytest.approx(0.999 * 0.25) and p.open_amount == pytest.approx(0.999 * 0.75)
    c.ex.sell_fill_fraction = 1.0
    c.ex.price = 102.0
    p = b.close_position(p, 102.0, "time_stop", 4)
    assert p.status == "closed"
    assert p.exit_price == pytest.approx(0.25 * 100 + 0.75 * 102)  # volume-weighted


def test_close_refuses_to_sell_coins_that_are_gone():
    c, b, p = opened()
    c.ex.free["BTC"] = 0.0  # sold manually on the exchange
    with pytest.raises(ExecutionError, match="forget"):
        b.close_position(p, 100.0, "manual", 3)


def test_move_stop_is_bot_managed():
    c, b, p = opened()
    b.protect(p, 2)
    n = len(c.ex.calls)
    b.move_stop(p, 100.0)
    assert p.stop_loss == 100.0 and len(c.ex.calls) == n  # no cancel/replace window


# -------------------------------------------------------------------- sync
def test_sync_records_exchange_stop_fill():
    c, b, p = opened()
    b.protect(p, 2)
    c.ex.trigger_stop(p.sl_order_id, 94.9)
    assert b.sync([p], 5) == []
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.9)


def test_sync_replaces_a_vanished_stop():
    c, b, p = opened()
    b.protect(p, 2)
    old = p.sl_order_id
    c.ex.cancel_order(old, "BTC/USDT")  # cancelled by someone else
    assert b.sync([p], 5) == []
    assert p.sl_order_id and p.sl_order_id != old


def test_sync_flags_coins_missing_and_equity_ignores_them():
    """Review #13: coins sold manually must not be counted or trusted."""
    c, b, p = opened()
    usdt = c.ex.free["USDT"]
    c.ex.free["BTC"] = 0.0
    issues = b.sync([p], 5)
    assert [i.kind for i in issues] == ["mismatch"]
    assert b.equity({"BTC/USDT": 100.0}, [p]) == pytest.approx(usdt)


def test_live_rejects_short():
    c, b = live()
    with pytest.raises(NotFilled):
        b.open_position(pending("short", mode="live"), 100.0, 1)


def test_partly_filled_stop_is_not_a_mismatch():
    c, b, p = opened()
    b.protect(p, 2)
    o = c.ex.orders[p.sl_order_id]
    o["filled"] = 0.4  # stop-limit partly executed, remainder still working
    c.ex.locked["BTC"] -= 0.4
    assert b.sync([p], 5) == []
