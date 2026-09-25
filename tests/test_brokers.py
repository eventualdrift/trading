import pytest

from tradebot.backtest.engine import Costs
from tradebot.db import Database
from tradebot.execution import LiveBroker, PaperBroker
from tradebot.models import Signal


def sig(side="long"):
    if side == "long":
        return Signal("BTC/USDT", "1h", "trend", "long", 100.0, 95.0, 110.0, 0, 0, 0, 10)
    return Signal("BTC/USDT", "1h", "trend", "short", 100.0, 105.0, 90.0, 0, 0, 0, 10)


def test_paper_long_round_trip(tmp_path):
    db = Database(tmp_path / "t.db")
    b = PaperBroker(db, Costs(0.001, 0.0005), 1000)
    p = b.open_position(sig(), 2.0, 100.0, 1)
    assert p.entry_price == pytest.approx(100.05)
    assert b.cash == pytest.approx(1000 - 100.05 * 2 * 0.001)
    assert b.equity({"BTC/USDT": 105.0}, [p]) == pytest.approx(b.cash + (105 - 100.05) * 2)
    p = b.close_position(p, 110.0, "take_profit", 2, limit_fill=True)
    assert p.exit_price == 110.0
    expected_pnl = (110 - 100.05) * 2 - 100.05 * 2 * 0.001 - 110 * 2 * 0.001
    assert p.pnl == pytest.approx(expected_pnl)
    assert b.cash == pytest.approx(1000 + expected_pnl)
    assert p.r_multiple == pytest.approx(expected_pnl / ((100.05 - 95) * 2))


def test_paper_short_stop(tmp_path):
    db = Database(tmp_path / "t.db")
    b = PaperBroker(db, Costs(0.0, 0.0), 1000)
    p = b.open_position(sig("short"), 1.0, 100.0, 1)
    p = b.close_position(p, 105.0, "stop_loss", 2)
    assert p.pnl == pytest.approx(-5.0) and p.r_multiple == pytest.approx(-1.0)
    assert b.cash == pytest.approx(995.0)


class FakeExchange:
    def __init__(self):
        self.orders = {}
        self.created = []
        self.cancelled = []
        self.balance = {"free": {"USDT": 1000.0, "BTC": 0.0}, "total": {"USDT": 1000.0, "BTC": 0.0}}
        self.fail_stop_types = set()

    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = params or {}
        oid = str(len(self.orders) + 1)
        if "stopLossPrice" in params:
            if type in self.fail_stop_types:
                import ccxt
                raise ccxt.InvalidOrder("not supported")
            o = {"id": oid, "status": "open", "type": type, "side": side, "amount": amount, "stop": params["stopLossPrice"]}
        else:
            px = 100.0 if side == "buy" else 108.0
            fee = {"currency": "BTC", "cost": amount * 0.001} if side == "buy" else {"currency": "USDT", "cost": amount * px * 0.001}
            o = {"id": oid, "status": "closed", "filled": amount, "average": px, "fee": fee, "side": side}
            if side == "buy":
                self.balance["free"]["BTC"] += amount - fee["cost"]
        self.orders[oid] = o
        self.created.append((type, side, amount, dict(params)))
        return dict(o)

    def fetch_order(self, oid, symbol=None):
        import ccxt
        if oid not in self.orders:
            raise ccxt.OrderNotFound(oid)
        return dict(self.orders[oid])

    def cancel_order(self, oid, symbol=None):
        self.orders[oid]["status"] = "canceled"
        self.cancelled.append(oid)

    def fetch_balance(self):
        return self.balance


class FakeClient:
    id = "fake"

    def __init__(self):
        self.ex = FakeExchange()

    def market(self, symbol):
        return {"base": "BTC", "quote": "USDT"}

    def limits(self, symbol):
        return {"min_amount": 0.0001, "min_cost": 5.0}

    def amount_to_precision(self, symbol, a):
        return float(f"{a:.6f}")

    def price_to_precision(self, symbol, p):
        return float(f"{p:.2f}")


def test_live_open_places_market_buy_and_exchange_stop():
    c = FakeClient()
    b = LiveBroker(c, "USDT")
    p = b.open_position(sig(), 1.0, 100.0, 1)
    assert c.ex.created[0][:2] == ("market", "buy")
    assert p.amount == pytest.approx(0.999)  # fee paid in BTC
    assert p.entry_price == 100.0
    stop = c.ex.orders[p.sl_order_id]
    assert stop["stop"] == 95.0 and stop["side"] == "sell" and stop["amount"] == pytest.approx(0.999)


def test_live_stop_falls_back_to_stop_limit_then_bot_managed():
    c = FakeClient()
    c.ex.fail_stop_types = {"market"}
    p = LiveBroker(c, "USDT").open_position(sig(), 1.0, 100.0, 1)
    assert c.ex.orders[p.sl_order_id]["type"] == "limit"
    c2 = FakeClient()
    c2.ex.fail_stop_types = {"market", "limit"}
    p2 = LiveBroker(c2, "USDT").open_position(sig(), 1.0, 100.0, 1)
    assert p2.sl_order_id is None


def test_live_close_cancels_stop_then_sells():
    c = FakeClient()
    b = LiveBroker(c, "USDT")
    p = b.open_position(sig(), 1.0, 100.0, 1)
    p = b.close_position(p, 108.0, "take_profit", 2)
    assert p.sl_order_id in c.ex.cancelled
    assert c.ex.created[-1][:3] == ("market", "sell", pytest.approx(0.999))
    assert p.status == "closed" and p.exit_price == 108.0
    assert p.pnl == pytest.approx((108 - 100) * 0.999 - 108 * 0.999 * 0.001)


def test_live_detects_exchange_stop_fill():
    c = FakeClient()
    b = LiveBroker(c, "USDT")
    p = b.open_position(sig(), 1.0, 100.0, 1)
    c.ex.orders[p.sl_order_id].update(status="closed", average=94.9, filled=0.999, fee=None)
    closed = b.sync([p], 5)
    assert closed == [p] and p.exit_reason == "stop_loss" and p.exit_price == 94.9
    # and a close attempt after the stop filled records the stop fill, no double sell
    c2 = FakeClient()
    b2 = LiveBroker(c2, "USDT")
    p2 = b2.open_position(sig(), 1.0, 100.0, 1)
    c2.ex.orders[p2.sl_order_id].update(status="closed", average=95.0)
    n_orders = len(c2.ex.created)
    p2 = b2.close_position(p2, 96.0, "exit_signal", 3)
    assert len(c2.ex.created) == n_orders and p2.exit_reason == "stop_loss"


def test_live_rejects_short():
    with pytest.raises(ValueError):
        LiveBroker(FakeClient(), "USDT").open_position(sig("short"), 1.0, 100.0, 1)


def test_live_move_stop_replaces_order():
    c = FakeClient()
    b = LiveBroker(c, "USDT")
    p = b.open_position(sig(), 1.0, 100.0, 1)
    old = p.sl_order_id
    b.move_stop(p, 100.0)
    assert old in c.ex.cancelled and p.sl_order_id != old
    assert c.ex.orders[p.sl_order_id]["stop"] == 100.0
