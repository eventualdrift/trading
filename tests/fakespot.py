"""A fake Binance spot exchange with the semantics the live broker relies on (sourced from the
Binance spot API docs; see tradebot/execution/live.py):

* a resting sell order locks the coins it would sell, so two full-quantity sells can't coexist;
* an OCO pair (orderList/oco) is two legs - TAKE_PROFIT above the last price, STOP_LOSS below it -
  sharing one quantity and one lock; a leg that triggers sells at market and expires the other;
  cancelling either leg cancels the pair;
* cancel-replace cancels one order (a leg cancels its pair) and places one new single order
  straight after, in one request; STOP_ON_FAILURE: a failed cancel places nothing (-2022); a
  refused new order after a successful cancel leaves nothing (-2021); both come back as
  ccxt.BadResponse('binance {json}'), like ccxt 4.5 raises them;
* a STOP_LOSS sell at or above the last price is refused ('Order would trigger immediately.');
* orders are found by client id; an unknown id raises OrderNotFound;
* ``fail[op]`` injects one failure per op: ('before', exc) - nothing happens; ('after', exc) - it
  happens and the answer is lost.

``coverage()`` is the quantity protected by stop orders right now; ``coverage_log`` records it
after every request, so a test can prove no request left a position without a stop.
"""
from __future__ import annotations

import json
from collections import defaultdict

import ccxt

TESTNET = "https://testnet.binance.vision/api/v3"


class FakeSpot:
    def __init__(self, prices=None, quote_balance=10_000.0, balances=None, lot=1e-6):
        """``balances``: other coins already on the account (the testnet funds every account with
        some); ``lot``: the quantity step (Binance BTCUSDT: 0.00001)."""
        self.prices = dict(prices or {"BTC/USDT": 100.0, "ETH/USDT": 10.0})
        self.lot = lot
        self.options = {"recvWindow": 10_000}
        self.urls = {"api": {"public": TESTNET, "private": TESTNET, "v1": "https://testnet.binance.vision/api/v1"}}
        self.free = defaultdict(float, {"USDT": float(quote_balance), **(balances or {})})
        self.locked = defaultdict(float)
        self.orders: dict[str, dict] = {}
        self.by_cid: dict[str, str] = {}
        self.lists: dict[int, list[str]] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, tuple] = {}
        self.down = 0  # the next N reads fail (the exchange unreachable for a while)
        self.fee = 0.001
        self.bnb_fees = False  # market buys pay their fee in BNB (Binance's fee discount)
        self.fill_fraction = 1.0  # a triggered or market order fills this share; the rest expires
        self.last_http_response = None
        self.coverage_log: list[tuple[str, dict]] = []

    # ------------------------------------------------------------ helpers
    @staticmethod
    def base(symbol):
        return symbol.split("/")[0]

    def market(self, symbol):
        b, q = symbol.split("/")
        return {"id": b + q, "base": b, "quote": q, "symbol": symbol}

    def market_id(self, symbol):
        return self.market(symbol)["id"]

    def symbol_of(self, market_id):
        return next(s for s in self.prices if self.market_id(s) == market_id)

    def amount_to_precision(self, symbol, a):
        steps = int(float(a) / self.lot + 1e-9)
        return f"{steps * self.lot:.8f}"

    @staticmethod
    def price_to_precision(symbol, p):
        return f"{float(p):.2f}"

    def _injected(self, op):
        when, exc = self.fail.pop(op, (None, None))
        if when == "before":
            raise exc
        return when, exc

    def _new_id(self):
        return str(len(self.orders) + 1)

    def _pub(self, o, with_fee=True):
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        if not with_fee:  # GET /api/v3/order has no fills or commission: ccxt gives fee None
            out["fee"] = None
        out["remaining"] = o["amount"] - o["filled"]
        out["info"] = {"orderListId": o.get("_list", -1), "type": o["_btype"]}
        return out

    def coverage(self, symbol="BTC/USDT"):
        """Quantity covered by an open stop on ``symbol`` (an OCO's stop leg counts once)."""
        return sum(o["amount"] - o["filled"] for o in self.orders.values()
                   if o["symbol"] == symbol and o["status"] == "open" and o["_btype"] == "STOP_LOSS")

    def _log(self, op):
        self.calls.append(op)
        self.coverage_log.append((op[0], {s: self.coverage(s) for s in self.prices}))

    def _lock(self, symbol, qty):
        b = self.base(symbol)
        if qty > self.free[b] + 1e-9:
            raise ccxt.InsufficientFunds("binance Account has insufficient balance for requested action.")
        self.free[b] -= qty
        self.locked[b] += qty

    def _algo(self, symbol, btype, qty, trigger, cid, list_id=None, locked=True):
        oid = self._new_id()
        o = {"id": oid, "symbol": symbol, "side": "sell", "type": "market", "amount": qty, "filled": 0.0,
             "status": "open", "clientOrderId": cid, "average": None, "fee": None, "triggerPrice": trigger,
             "_btype": btype, "_list": list_id if list_id is not None else -1, "_locked": locked}
        if btype == "TAKE_PROFIT":
            o["takeProfitPrice"] = trigger
        self.orders[oid] = o
        if cid:
            self.by_cid[cid] = oid
        return o

    def _release(self, o):
        """Unlock what an order (or its pair) still holds."""
        if o.get("_list", -1) >= 0:
            legs = [self.orders[i] for i in self.lists[o["_list"]]]
            left = max(x["amount"] - x["filled"] for x in legs)
            if any(x["status"] == "open" for x in legs):
                self.free[self.base(o["symbol"])] += left
                self.locked[self.base(o["symbol"])] -= left
            return
        if o["status"] != "open":
            return
        left = o["amount"] - o["filled"]
        if o.get("_locked"):
            self.free[self.base(o["symbol"])] += left
            self.locked[self.base(o["symbol"])] -= left
        elif o["_btype"] == "LIMIT" and o["side"] == "buy":  # a resting buy holds quote
            self.free["USDT"] += left * o["price"]
            self.locked["USDT"] -= left * o["price"]

    def _cancel_order(self, o):
        """-> True if it was open and is now cancelled (a leg cancels its pair)."""
        if o["status"] != "open":
            return False
        self._release(o)
        for leg_id in (self.lists[o["_list"]] if o.get("_list", -1) >= 0 else [o["id"]]):
            if self.orders[leg_id]["status"] == "open":
                self.orders[leg_id]["status"] = "canceled"
        return True

    def _would_trigger(self, symbol, btype, trigger):
        p = self.prices[symbol]
        return trigger >= p if btype == "STOP_LOSS" else trigger <= p

    # ------------------------------------------------------------ orders
    def create_order(self, symbol, type, side, amount, price=None, params=None):
        params = dict(params or {})
        trigger = params.get("stopLossPrice") or params.get("triggerPrice") or params.get("stopPrice")
        op = "stop" if trigger else side
        when, exc = self._injected(op)
        cid = params.get("clientOrderId")
        b = self.base(symbol)
        if trigger:
            trigger = float(trigger)
            if self._would_trigger(symbol, "STOP_LOSS", trigger):
                raise ccxt.OrderImmediatelyFillable("binance Order would trigger immediately.")
            self._lock(symbol, amount)
            o = self._algo(symbol, "STOP_LOSS", float(amount), trigger, cid)
            o["type"] = type
        elif type == "limit":
            oid = self._new_id()
            o = {"id": oid, "symbol": symbol, "side": side, "type": "limit", "amount": float(amount), "filled": 0.0,
                 "status": "open", "clientOrderId": cid, "average": None, "fee": None, "price": price,
                 "_btype": "LIMIT", "_list": -1, "_locked": side == "sell"}
            if side == "buy":
                self.free["USDT"] -= amount * price
                self.locked["USDT"] += amount * price
            else:
                self._lock(symbol, amount)
            self.orders[oid] = o
            if cid:
                self.by_cid[cid] = oid
        else:
            px = self.prices[symbol]
            oid = self._new_id()
            filled = float(amount) * self.fill_fraction
            if side == "buy":
                if filled * px > self.free["USDT"] + 1e-9:
                    raise ccxt.InsufficientFunds("binance Account has insufficient balance for requested action.")
                self.free["USDT"] -= filled * px
                if self.bnb_fees:  # the fee discount: paid in BNB, the coins arrive whole
                    fee_d = {"currency": "BNB", "cost": filled * px * self.fee * 0.75 / self.prices.get("BNB/USDT", 500.0)}
                    self.free["BNB"] -= fee_d["cost"]
                    self.free[b] += filled
                else:
                    fee = filled * self.fee
                    self.free[b] += filled - fee
                    fee_d = {"currency": b, "cost": fee}
            else:
                if float(amount) > self.free[b] + 1e-9:
                    raise ccxt.InsufficientFunds("binance Account has insufficient balance for requested action.")
                self.free[b] -= filled
                self.free["USDT"] += filled * px * (1 - self.fee)
                fee_d = {"currency": "USDT", "cost": filled * px * self.fee}
            o = {"id": oid, "symbol": symbol, "side": side, "type": "market", "amount": float(amount), "filled": filled,
                 "status": "closed" if filled >= float(amount) else "expired", "clientOrderId": cid, "average": px,
                 "fee": fee_d, "_btype": "MARKET", "_list": -1}
            self.orders[oid] = o
            if cid:
                self.by_cid[cid] = oid
        self._log(("create", op, params))
        if when == "after":
            raise exc
        return self._pub(o)

    def privatePostOrderListOco(self, params):
        when, exc = self._injected("oco")
        symbol = self.symbol_of(params["symbol"])
        qty = float(params["quantity"])
        tp, stop = float(params["aboveStopPrice"]), float(params["belowStopPrice"])
        assert params["side"] == "SELL" and params["aboveType"] == "TAKE_PROFIT" and params["belowType"] == "STOP_LOSS"
        if not tp > self.prices[symbol] > stop:
            raise ccxt.InvalidOrder("binance The relationship of the prices for the orders is not correct.")
        self._lock(symbol, qty)
        list_id = len(self.lists) + 1
        t = self._algo(symbol, "TAKE_PROFIT", qty, tp, params.get("aboveClientOrderId"), list_id)
        s = self._algo(symbol, "STOP_LOSS", qty, stop, params.get("belowClientOrderId"), list_id)
        self.lists[list_id] = [t["id"], s["id"]]
        self._log(("oco", params))
        if when == "after":
            raise exc
        return {"orderListId": list_id, "listClientOrderId": params.get("listClientOrderId"),
                "orders": [{"symbol": params["symbol"], "orderId": int(x["id"]), "clientOrderId": x["clientOrderId"]}
                           for x in (s, t)]}

    def _bad(self, body):
        self.last_http_response = json.dumps(body)
        raise ccxt.BadResponse("binance " + json.dumps(body))

    def privatePostOrderCancelReplace(self, params):
        when, exc = self._injected("replace")
        symbol = self.symbol_of(params["symbol"])
        assert params["cancelReplaceMode"] == "STOP_ON_FAILURE" and params["type"] == "STOP_LOSS"
        old = self.orders.get(str(params["cancelOrderId"]))
        if old is None or old["status"] != "open":
            self._log(("replace", params, "cancel failed"))
            self._bad({"code": -2022, "msg": "Order cancel-replace failed.",
                       "data": {"cancelResult": "FAILURE", "newOrderResult": "NOT_ATTEMPTED",
                                "cancelResponse": {"code": -2011, "msg": "Unknown order sent."}, "newOrderResponse": None}})
        self._cancel_order(old)
        stop, qty = float(params["stopPrice"]), float(params["quantity"])
        refusal = None
        if self.fail.pop("replace_new", None):
            refusal = {"code": -2010, "msg": "Account has insufficient balance for requested action."}
        elif self._would_trigger(symbol, "STOP_LOSS", stop):
            refusal = {"code": -2010, "msg": "Order would trigger immediately."}
        if refusal:
            self._log(("replace", params, "new refused"))
            self._bad({"code": -2021, "msg": "Order cancel-replace partially failed.",
                       "data": {"cancelResult": "SUCCESS", "newOrderResult": "FAILURE",
                                "cancelResponse": {"orderId": int(old["id"]), "status": "CANCELED"},
                                "newOrderResponse": refusal}})
        self._lock(symbol, qty)
        new = self._algo(symbol, "STOP_LOSS", qty, stop, params.get("newClientOrderId"))
        self._log(("replace", params, "ok"))
        if when == "after":
            raise exc
        return {"cancelResult": "SUCCESS", "newOrderResult": "SUCCESS",
                "cancelResponse": {"orderId": int(old["id"]), "status": "CANCELED"},
                "newOrderResponse": {"orderId": int(new["id"]), "clientOrderId": new["clientOrderId"], "status": "NEW"}}

    def fetch_order(self, oid, symbol=None, params=None):
        params = params or {}
        when, exc = self._injected("fetch")
        if self.down:
            self.down -= 1
            raise ccxt.ExchangeNotAvailable("binance 503 Service Unavailable")
        if oid is None:
            oid = self.by_cid.get(params.get("clientOrderId"))
        if oid not in self.orders:
            raise ccxt.OrderNotFound("binance Order does not exist.")
        return self._pub(self.orders[oid], with_fee=False)

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params=None):
        """The order's trades (GET /api/v3/myTrades?orderId=): where the fees are."""
        self.calls.append(("trades", (params or {}).get("orderId")))
        o = self.orders.get(str((params or {}).get("orderId")))
        if o is None or not o["filled"]:
            return []
        return [{"order": o["id"], "amount": o["filled"], "price": o["average"], "fee": o["fee"]}]

    def cancel_order(self, oid, symbol=None, params=None):
        self._injected("cancel")
        o = self.orders.get(str(oid))
        if o is None or o["status"] != "open":
            raise ccxt.OrderNotFound("binance Unknown order sent.")
        self._cancel_order(o)
        self._log(("cancel", oid))

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        return [self._pub(o) for o in self.orders.values()
                if o["status"] == "open" and (symbol is None or o["symbol"] == symbol)]

    def fetch_balance(self):
        keys = set(self.free) | set(self.locked)
        return {"free": {k: self.free[k] for k in keys}, "used": {k: self.locked[k] for k in keys},
                "total": {k: self.free[k] + self.locked[k] for k in keys}}

    # ------------------------------------------------------------ the market
    def set_price(self, symbol, price):
        """The last trade moves: algo orders whose condition it meets trigger and sell at market."""
        self.prices[symbol] = float(price)
        b = self.base(symbol)
        for o in list(self.orders.values()):
            if o["symbol"] != symbol or o["status"] != "open" or o["_btype"] not in ("STOP_LOSS", "TAKE_PROFIT"):
                continue
            hit = price <= o["triggerPrice"] if o["_btype"] == "STOP_LOSS" else price >= o["triggerPrice"]
            if not hit:
                continue
            qty = o["amount"] - o["filled"]
            filled = qty * self.fill_fraction
            self.locked[b] -= qty
            self.free[b] += qty - filled  # an unfilled remainder expires back to free coins
            self.free["USDT"] += filled * price * (1 - self.fee)
            o.update(filled=o["filled"] + filled, average=price, status="closed" if filled >= qty else "expired",
                     fee={"currency": "USDT", "cost": filled * price * self.fee})
            for leg_id in (self.lists[o["_list"]] if o.get("_list", -1) >= 0 else []):
                sib = self.orders[leg_id]
                if sib is not o and sib["status"] == "open":
                    sib["status"] = "expired"  # the pair's other leg: expired by the exchange, not cancelled


class FakeSpotClient:
    """ExchangeClient's interface over FakeSpot (prices, precision, limits, the testnet guard)."""

    id = "binance"

    def __init__(self, prices=None, sandbox=True, quote_balance=10_000.0, balances=None, lot=1e-6):
        self.ex = FakeSpot(prices, quote_balance, balances, lot)
        self.sandbox = sandbox
        if not sandbox:
            self.ex.urls = {"api": {"public": "https://api.binance.com/api/v3", "private": "https://api.binance.com/api/v3"}}

    def market(self, symbol):
        return self.ex.market(symbol)

    def limits(self, symbol):
        return {"min_amount": 0.00001, "min_cost": 5.0}

    def amount_to_precision(self, symbol, a):
        return float(self.ex.amount_to_precision(symbol, a))

    def price_to_precision(self, symbol, p):
        return float(self.ex.price_to_precision(symbol, p))

    def fetch_last_price(self, symbol):
        return self.ex.prices[symbol]

    def assert_testnet(self):
        from tradebot.data.exchange import ExchangeClient

        ExchangeClient.assert_testnet(self)
