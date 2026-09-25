import ccxt
import pytest

from tradebot.backtest.engine import Costs
from tradebot.db import Database
from tradebot.execution import LiveBroker, PaperBroker
from tradebot.execution.base import ExecutionError, NotFilled, ProtectionError
from tradebot.models import Position, Signal


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    monkeypatch.setattr("tradebot.data.exchange.time.sleep", lambda s: None)


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


# ------------------------------------------------------------ fake Binance
class FakeBinance:
    """Spot exchange with Binance semantics: stops are ordinary orders that lock coins,
    orders are findable by client id, and unknown ids raise OrderNotFound."""

    def __init__(self, price=100.0):
        self.price = price
        self.options = {"recvWindow": 10_000}
        self.orders: dict[str, dict] = {}
        self.by_cid: dict[str, str] = {}
        self.calls = []
        self.free = {"USDT": 10_000.0, "BTC": 0.0}
        self.locked = {"BTC": 0.0}
        self.stop_mode = "normal"  # normal | ignore | no_trigger | short_qty | reject_market | reject_all
        self.buy_fill = 1.0
        self.sell_fill = 1.0
        self.partial_status = "open"  # what a partly filled market order reports: open | expired
        self.fail = {}  # "buy"|"sell"|"stop" -> ("before"|"after", exception), one-shot
        self.fetch_failures = 0
        self.on_create = None
        self.on_cancel_stop = None

    def _pub(self, o):
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        out["remaining"] = o["amount"] - o["filled"]
        return out

    def _sell_coins(self, qty, price, from_locked=False):
        (self.locked if from_locked else self.free)["BTC"] -= qty
        self.free["USDT"] += qty * price * 0.999

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        key = "stop" if "stopLossPrice" in params else side
        self.calls.append(("create", key, params))
        if self.on_create:
            self.on_create(key, params)
        when, exc = self.fail.pop(key, (None, None))
        if when == "before":
            raise exc
        oid = str(len(self.orders) + 1)
        o = {"id": oid, "symbol": symbol, "side": side, "type": type, "amount": amount, "filled": 0.0,
             "status": "open", "clientOrderId": params.get("clientOrderId"), "average": None, "fee": None}
        if key == "stop":
            if self.stop_mode == "reject_all" or (self.stop_mode == "reject_market" and type == "market"):
                raise ccxt.InvalidOrder("order type not supported for this symbol")
            if self.stop_mode == "ignore":  # executed as a plain sell
                self._sell_coins(amount, self.price)
                o.update(status="closed", filled=amount, average=self.price)
            else:
                qty = 0.1 if self.stop_mode == "short_qty" else amount
                self.free["BTC"] -= qty
                self.locked["BTC"] += qty
                o.update(amount=qty, _locked=True,
                         triggerPrice=None if self.stop_mode == "no_trigger" else params["stopLossPrice"])
        elif side == "buy":
            filled = amount * self.buy_fill
            fee = filled * 0.001
            self.free["BTC"] += filled - fee
            self.free["USDT"] -= filled * self.price
            o.update(filled=filled, average=self.price, fee={"currency": "BTC", "cost": fee},
                     status="closed" if filled >= amount else self.partial_status)
        else:
            if amount > self.free["BTC"] + 1e-12:
                raise ccxt.InsufficientFunds("Account has insufficient balance")
            filled = amount * self.sell_fill
            self._sell_coins(filled, self.price)
            o.update(filled=filled, average=self.price, fee={"currency": "USDT", "cost": filled * self.price * 0.001},
                     status="closed" if filled >= amount else self.partial_status)
        self.orders[oid] = o
        if o["clientOrderId"]:
            self.by_cid[o["clientOrderId"]] = oid
        if when == "after":
            raise exc
        return self._pub(o)

    def fetch_order(self, oid, symbol=None, params=None):
        params = params or {}
        self.calls.append(("fetch", oid, dict(params)))
        if self.fetch_failures:
            self.fetch_failures -= 1
            raise ccxt.NetworkError("connection reset")
        if oid is None:
            oid = self.by_cid.get(params.get("clientOrderId"))
        if oid not in self.orders:
            raise ccxt.OrderNotFound("Order does not exist.")
        return self._pub(self.orders[oid])

    def cancel_order(self, oid, symbol=None, params=None):
        self.calls.append(("cancel", oid, dict(params or {})))
        o = self.orders.get(oid)
        if o is None:
            raise ccxt.OrderNotFound(oid)
        if o.get("_locked") and self.on_cancel_stop:
            self.on_cancel_stop(o)
        if o["status"] != "open":
            raise ccxt.OrderNotFound("Unknown order sent.")
        if o.get("_locked"):
            left = o["amount"] - o["filled"]
            self.free["BTC"] += left
            self.locked["BTC"] -= left
        o["status"] = "canceled"

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return [self._pub(o) for o in self.orders.values() if o["status"] == "open"]

    def trigger_stop(self, oid, price):
        o = self.orders[oid]
        self._sell_coins(o["amount"], price, from_locked=True)
        o.update(status="closed", filled=o["amount"], average=price)

    def fetch_balance(self):
        total = {k: self.free.get(k, 0.0) + self.locked.get(k, 0.0) for k in set(self.free) | set(self.locked)}
        return {"free": dict(self.free), "total": total}

    def open_sells(self):
        return [o for o in self.orders.values() if o["side"] == "sell" and o["status"] == "open" and not o.get("_locked")]


class FakeClient:
    def __init__(self, venue="binance", price=100.0):
        self.id = venue
        self.ex = FakeBinance(price)

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


class Clock:
    def __init__(self):
        self.t = 1_700_000_000_000

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += int(s * 1000)


def live(venue="binance"):
    c, clock = FakeClient(venue), Clock()
    saved = []
    b = LiveBroker(c, "USDT", sleep=clock.sleep, clock=clock, fill_timeout_s=2,
                   persist=lambda p: saved.append({k: getattr(p, k) for k in
                                                   ("client_order_id", "sl_client_id", "exit_client_id",
                                                    "entry_order_id", "sl_order_id", "exit_order_id")}))
    b.saved = saved
    return c, b


def opened(amount=1.0):
    c, b = live()
    p = b.open_position(pending(amount=amount, mode="live"), 100.0, 1)
    return c, b, p


def protected():
    c, b, p = opened()
    b.protect(p, 2)
    assert p.sl_order_id
    return c, b, p


def sells(c):
    return [x for x in c.ex.calls if x[0] == "create" and x[1] == "sell"]


# ------------------------------------------------------------------- venue
@pytest.mark.parametrize("venue", ["okx", "bybit", "kraken", "someexchange"])
def test_only_verified_venues_trade_live(venue):
    """Review: OKX trigger orders and Bybit account modes need their own adapters."""
    with pytest.raises(ValueError, match="not supported"):
        LiveBroker(FakeClient(venue), "USDT")


# ------------------------------------------------------------------- entry
def test_client_id_is_persisted_before_the_order_is_sent():
    c, b = live()
    p = pending(mode="live")
    seen = []
    c.ex.on_create = lambda key, params: seen.append((params["clientOrderId"], p.client_order_id))
    b.open_position(p, 100.0, 1)
    cid, persisted_cid = seen[0]
    assert cid == persisted_cid == p.client_order_id  # the pending row already carries it
    assert p.entry_order_id and b.saved[-1]["entry_order_id"] == p.entry_order_id


def test_entry_fills_and_coin_fee():
    c, b, p = opened()
    assert p.status == "open" and p.amount == pytest.approx(0.999) and p.fees == pytest.approx(0.1)


def test_base_fee_in_realized_pnl():
    c, b, p = opened()
    c.ex.price = 108.0
    p = b.close_position(p, 108.0, "take_profit", 2)
    assert p.pnl == pytest.approx(0.999 * 108 - 100.0 - 0.999 * 108 * 0.001)


def test_entry_rejected_is_not_filled():
    c, b = live()
    c.ex.fail["buy"] = ("before", ccxt.InsufficientFunds("no money"))
    with pytest.raises(NotFilled):
        b.open_position(pending(mode="live"), 100.0, 1)


def test_timeout_after_execution_is_found_by_client_id():
    c, b = live()
    c.ex.fail["buy"] = ("after", ccxt.RequestTimeout("read timed out"))
    p = b.open_position(pending(mode="live"), 100.0, 1)
    assert p.status == "open" and p.amount == pytest.approx(0.999) and p.entry_order_id


def test_timeout_before_execution_ignores_unrelated_deposit():
    """Review: a balance change must never be attributed to an order."""
    c, b = live()
    c.ex.fail["buy"] = ("before", ccxt.RequestTimeout("read timed out"))
    c.ex.free["BTC"] += 5.0  # a deposit lands meanwhile
    with pytest.raises(NotFilled):
        b.open_position(pending(mode="live"), 100.0, 1)


def test_timed_out_partial_buy_is_settled_so_nothing_arrives_later():
    """Review: 50% filled, the rest still working when the response was lost."""
    c, b = live()
    c.ex.buy_fill = 0.5
    c.ex.fail["buy"] = ("after", ccxt.RequestTimeout("read timed out"))
    p = b.open_position(pending(mode="live"), 100.0, 1)
    assert p.amount == pytest.approx(0.4995)
    assert c.ex.orders[p.entry_order_id]["status"] == "canceled"  # remainder can't fill later


def test_entry_outcome_unknown_when_the_exchange_cannot_be_asked():
    c, b = live()
    c.ex.fail["buy"] = ("after", ccxt.RequestTimeout("timeout"))
    c.ex.fetch_failures = 100
    with pytest.raises(ccxt.NetworkError):  # neither filled nor failed: the caller marks it unknown
        b.open_position(pending(mode="live"), 100.0, 1)


def test_reconcile_entry_by_client_id():
    c, b = live()
    p = pending(mode="live")
    c.ex.create_order("BTC/USDT", "market", "buy", 1.0, None, {"clientOrderId": p.client_order_id})
    assert b.reconcile_entry(p, 5) == "open" and p.amount == pytest.approx(0.999)
    q = pending(mode="live")  # never reached the exchange
    assert b.reconcile_entry(q, 5) == "failed"


# -------------------------------------------------------------- protection
def test_protect_places_and_verifies_stop():
    c, b, p = protected()
    o = c.ex.orders[p.sl_order_id]
    assert o["status"] == "open" and o["triggerPrice"] == 95.0 and o["amount"] == pytest.approx(0.999)
    assert o["clientOrderId"] == p.sl_client_id


def test_stop_covering_too_little_is_rejected():
    """Review: verification must check quantity."""
    c, b, p = opened()
    c.ex.stop_mode = "short_qty"
    with pytest.raises(ProtectionError, match="covers"):
        b.protect(p, 2)
    assert not [o for o in c.ex.orders.values() if o["status"] == "open"]


def test_stop_accepted_before_timeout_is_adopted_not_orphaned():
    """Review: the stop exists even though the response was lost."""
    c, b, p = opened()
    c.ex.fail["stop"] = ("after", ccxt.RequestTimeout("read timed out"))
    b.protect(p, 2)
    assert p.sl_order_id and c.ex.orders[p.sl_order_id]["status"] == "open"
    assert len([o for o in c.ex.orders.values() if o.get("_locked")]) == 1


def test_stop_timeout_before_acceptance_falls_back():
    c, b, p = opened()
    c.ex.fail["stop"] = ("before", ccxt.RequestTimeout("read timed out"))
    b.protect(p, 2)
    assert c.ex.orders[p.sl_order_id]["type"] == "limit"


def test_stop_limit_fallback_then_failure():
    c, b, p = opened()
    c.ex.stop_mode = "reject_market"
    b.protect(p, 2)
    assert c.ex.orders[p.sl_order_id]["type"] == "limit"
    c2, b2, p2 = opened()
    c2.ex.stop_mode = "reject_all"
    with pytest.raises(ProtectionError):
        b2.protect(p2, 2)


def test_stop_that_executes_immediately_is_not_protection():
    c, b, p = opened()
    c.ex.stop_mode = "ignore"
    b.protect(p, 2)
    assert p.sl_order_id is None and p.status == "closed" and p.exit_reason == "stop_order_executed"


def test_open_order_without_trigger_is_cancelled():
    c, b, p = opened()
    c.ex.stop_mode = "no_trigger"
    with pytest.raises(ProtectionError):
        b.protect(p, 2)
    assert not [o for o in c.ex.orders.values() if o["status"] == "open"]
    assert c.ex.free["BTC"] == pytest.approx(0.999)


# -------------------------------------------------------------------- exit
def test_close_cancels_stop_then_sells_once():
    c, b, p = protected()
    c.ex.price = 108.0
    p = b.close_position(p, 108.0, "take_profit", 3)
    assert len(sells(c)) == 1 and p.exit_price == pytest.approx(108.0) and c.ex.free["BTC"] == pytest.approx(0.0)
    assert not [o for o in c.ex.orders.values() if o["status"] == "open"]


def test_stop_filling_during_cancel_never_double_sells():
    c, b, p = protected()
    c.ex.on_cancel_stop = lambda o: c.ex.trigger_stop(o["id"], 94.9)
    p = b.close_position(p, 96.0, "exit_signal", 3)
    assert sells(c) == [] and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.9)


def test_partial_exit_keeps_position_open_and_retries():
    c, b, p = opened()
    c.ex.sell_fill = 0.25
    with pytest.raises(ExecutionError):
        b.close_position(p, 100.0, "time_stop", 3)
    assert p.status == "open" and p.exit_filled == pytest.approx(0.999 * 0.25)
    assert c.ex.open_sells() == []  # the unfilled rest was cancelled, not left working
    c.ex.sell_fill, c.ex.price = 1.0, 102.0
    p = b.close_position(p, 102.0, "time_stop", 4)
    assert p.status == "closed" and p.exit_price == pytest.approx(0.25 * 100 + 0.75 * 102)


def test_unconfirmed_partial_sell_is_resolved_before_any_new_sell():
    """Review: the first sell (25% filled, rest working) must be finished before retrying."""
    c, b, p = opened()
    c.ex.sell_fill = 0.25
    c.ex.fetch_failures = 100  # can't read the order back
    with pytest.raises(ccxt.NetworkError):
        b.close_position(p, 100.0, "time_stop", 3)
    first = p.exit_order_id
    assert first and c.ex.orders[first]["status"] == "open"
    c.ex.fetch_failures, c.ex.sell_fill = 0, 1.0
    c.ex.on_create = lambda key, params: (key != "sell") or (not c.ex.open_sells()) or pytest.fail("two sells live")
    p = b.close_position(p, 100.0, "time_stop", 4)
    assert p.status == "closed" and c.ex.orders[first]["status"] == "canceled"
    assert p.exit_filled == pytest.approx(0.999)


def test_sell_lost_before_reaching_exchange_is_retried_once_confirmed_absent():
    c, b, p = opened()
    c.ex.fail["sell"] = ("before", ccxt.RequestTimeout("timeout"))
    with pytest.raises(ExecutionError):
        b.close_position(p, 100.0, "time_stop", 3)
    assert p.exit_client_id
    p = b.close_position(p, 100.0, "time_stop", 4)
    assert p.status == "closed" and len([x for x in sells(c)]) == 2  # 1 lost + 1 real


def test_sell_executed_but_response_lost_is_not_repeated():
    c, b, p = opened()
    c.ex.fail["sell"] = ("after", ccxt.RequestTimeout("timeout"))
    with pytest.raises(ExecutionError):
        b.close_position(p, 100.0, "time_stop", 3)
    p = b.close_position(p, 100.0, "time_stop", 4)
    assert p.status == "closed" and len(sells(c)) == 1


def test_close_refuses_to_sell_coins_that_are_gone():
    c, b, p = opened()
    c.ex.free["BTC"] = 0.0
    with pytest.raises(ExecutionError, match="forget"):
        b.close_position(p, 100.0, "manual", 3)


def test_move_stop_is_bot_managed():
    c, b, p = protected()
    n = len(c.ex.calls)
    b.move_stop(p, 100.0)
    assert p.stop_loss == 100.0 and len(c.ex.calls) == n


# -------------------------------------------------------------------- sync
def test_sync_records_exchange_stop_fill():
    c, b, p = protected()
    c.ex.trigger_stop(p.sl_order_id, 94.9)
    assert b.sync([p], 5) == []
    assert p.status == "closed" and p.exit_reason == "stop_loss" and p.exit_price == pytest.approx(94.9)


def test_sync_does_not_believe_a_fill_the_balance_contradicts():
    """Review (OKX 'effective'): an order claiming a fill while the coins are still held."""
    c, b, p = protected()
    c.ex.orders[p.sl_order_id].update(status="closed", filled=0.999, average=95.0)  # coins untouched
    issues = b.sync([p], 5)
    assert p.status == "open" and [i.kind for i in issues] == ["error"]


def test_sync_replaces_a_vanished_stop():
    c, b, p = protected()
    old = p.sl_order_id
    c.ex.cancel_order(old, "BTC/USDT")
    assert b.sync([p], 5) == []
    assert p.sl_order_id and p.sl_order_id != old


def test_sync_flags_coins_missing_and_equity_ignores_them():
    c, b, p = opened()
    usdt = c.ex.free["USDT"]
    c.ex.free["BTC"] = 0.0
    assert [i.kind for i in b.sync([p], 5)] == ["mismatch"]
    assert b.equity({"BTC/USDT": 100.0}, [p]) == pytest.approx(usdt)


def test_partly_filled_stop_is_not_a_mismatch():
    c, b, p = protected()
    o = c.ex.orders[p.sl_order_id]
    o["filled"] = 0.4
    c.ex.locked["BTC"] -= 0.4
    assert b.sync([p], 5) == []


def test_orphan_sweep_cancels_only_unowned_bot_orders():
    c, b, p = protected()
    stray = c.ex.create_order("BTC/USDT", "market", "sell", 0.0, None, {"stopLossPrice": 90.0, "clientOrderId": "tbsSTRAY"})
    manual = c.ex.create_order("BTC/USDT", "limit", "buy", 0.0, 50.0, {"clientOrderId": "myownorder"})
    c.ex.orders[manual["id"]]["status"] = "open"
    done = b.cancel_orphans({"BTC/USDT"}, {p.sl_order_id})
    assert len(done) == 1 and c.ex.orders[stray["id"]]["status"] == "canceled"
    assert c.ex.orders[p.sl_order_id]["status"] == "open" and c.ex.orders[manual["id"]]["status"] == "open"


def test_live_rejects_short():
    c, b = live()
    with pytest.raises(NotFilled):
        b.open_position(pending("short", mode="live"), 100.0, 1)
