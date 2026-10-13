"""`tradebot testnet verify`: the go-live order paths, run on the exchange's testnet and checked
there. Never the real exchange: the client is built from the testnet keys (TESTNET_API_KEY /
TESTNET_API_SECRET) and refused unless every API URL it can call is the testnet's.

What it checks, each a step in the record (ok, failed, or inconclusive with the reason):

1. the account: the testnet answers, and has the quote balance the run needs;
2. an entry protected by an OCO pair - a take-profit trigger and a stop-loss on one quantity -
   read back from the exchange;
3. the stop moved to breakeven in ONE cancel-replace request: the pair is gone, exactly one stop
   stands at the new level, and no other order request was made for the move;
4. a trailing step, the same way;
5. a move to a level the price is already through: the exchange refuses the new stop after
   cancelling the old one, and the position is sold at once;
6. an exchange-side exit while the bot isn't looking: a pair with levels a few basis points from
   the price, left to the market (inconclusive if the testnet price doesn't reach either in time);
7. an order whose answer was lost is found by its client id, and one never sent is not;
8. the core sleeve: market buys and sells booked from their fills; a core order whose answer was
   lost is resolved by its client id;
9. clean-up: no bot order left open, the coins the run bought sold again.

The record goes to <state>/testnet/verify-<UTC time>.json with the code version.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

WATCHED = ("create_order", "cancel_order", "privatePostOrderListOco", "privatePostOrderCancelReplace")


@dataclass
class Step:
    name: str
    ok: bool | None  # None: inconclusive
    detail: str


class Recorder:
    """Passes everything through to the exchange object, and records the order requests and the
    client ids the run sent (clean-up touches only those)."""

    def __init__(self, ex):
        self.__dict__["_ex"] = ex
        self.__dict__["requests"] = []
        self.__dict__["client_ids"] = set()

    def __getattr__(self, name):
        attr = getattr(self._ex, name)
        if name in WATCHED and callable(attr):
            def call(*a, **k):
                self.requests.append(name)
                params = (a[-1] if a and isinstance(a[-1], dict) else None) or k.get("params") or {}
                for key in ("clientOrderId", "newClientOrderId", "aboveClientOrderId", "belowClientOrderId"):
                    if params.get(key):
                        self.client_ids.add(str(params[key]))
                return attr(*a, **k)
            return call
        return attr

    def __setattr__(self, name, value):
        setattr(self._ex, name, value)


def _position(symbol: str, price: float, amount: float, stop: float, tp: float, now_ms: int):
    from .models import Position, Signal

    sig = Signal(symbol, "1h", "verify", "long", price, stop, tp, now_ms, now_ms, now_ms + 3_600_000, now_ms + 86_400_000,
                 reason="testnet verify")
    return Position.from_signal(sig, "testnet", amount, now_ms)


def verify(client, quote: str = "USDT", symbol: str = "BTC/USDT", core_symbols=("BTC/USDT", "ETH/USDT"),
           budget: float = 20.0, wait_s: float = 600.0, sleep=time.sleep, clock=None, poll_s: float = 5.0,
           log=print, db_path=None, wait=None) -> list[Step]:
    """Run the checks on ``client`` (an ExchangeClient on the testnet). ``budget``: quote spent per
    trade; ``wait_s``: how long step 6 waits for the market, polling every ``poll_s`` (``wait``,
    default ``sleep``)."""
    from .backtest.engine import Costs
    from .config import CoreConfig
    from .core import CoreSleeve
    from .db import Database
    from .execution import LiveBroker
    from .models import new_client_id

    client.assert_testnet()  # before anything is sent
    clock = clock or (lambda: int(time.time() * 1000))
    wait = wait or sleep
    steps: list[Step] = []
    start_base: dict[str, float] = {}

    def step(name, ok, detail):
        steps.append(Step(name, ok, detail))
        log(f"  [{'ok' if ok else 'FAILED' if ok is False else 'inconclusive'}] {name}: {detail}")
        return ok

    rec = Recorder(client.ex)
    client.ex = rec
    broker = LiveBroker(client, quote, testnet=True, sleep=sleep, clock=clock, id_tag="v")  # its orders: 'tbv...'
    try:
        base = client.market(symbol)["base"]
        opened = [(s, o) for s in sorted({symbol, *core_symbols}) for o in rec.fetch_open_orders(s)]
        for s, o in opened:  # an earlier run's leftovers (a run killed before its clean-up): cancelled
            if str(o.get("clientOrderId") or "").startswith("tbv"):
                rec.cancel_order(str(o["id"]), s)
        busy = [o for s, o in opened if str(o.get("clientOrderId") or "").startswith("tb")
                and not str(o.get("clientOrderId") or "").startswith("tbv")]
        if busy:  # a testnet bot on these keys: its orders and the run's would get in each other's way
            step("account", False, f"{len(busy)} bot orders are open on this testnet account - a testnet bot seems "
                 f"to be running on the same keys; stop it before verifying")
            return steps
        start = rec.fetch_balance()
        free_quote = float((start.get("free") or {}).get(quote) or 0.0)
        start_base = {s: float((start.get("total") or {}).get(client.market(s)["base"]) or 0.0)
                      for s in {symbol, *core_symbols}}
        if not step("account", free_quote >= 4 * budget, f"{free_quote:,.2f} {quote} free on the testnet "
                    f"(the run spends about {4 * budget:,.0f})"):
            return steps

        # 2. entry and the pair
        price = client.fetch_last_price(symbol)
        pos = _position(symbol, price, budget / price, price * 0.97, price * 1.03, clock())
        broker.open_position(pos, price, clock())
        broker.protect(pos, clock())
        prot = pos.protection
        legs = [rec.fetch_order(i, symbol) for i in (pos.sl_order_id, prot.get("tp_order_id")) if i]
        info_types = sorted(str((leg.get("info") or {}).get("type")) for leg in legs)
        lists = {str((leg.get("info") or {}).get("orderListId")) for leg in legs}
        ok = (len(legs) == 2 and info_types == ["STOP_LOSS", "TAKE_PROFIT"] and len(lists) == 1
              and all(leg.get("status") == "open" for leg in legs))
        step("entry protected by an OCO pair", ok, f"bought {pos.amount:g} {base} at {pos.entry_price:,.2f}; legs "
             f"{info_types} in list {lists}, stop {prot.get('stop')}, take-profit {prot.get('tp')}")

        # 3, 4. moves: one request each, one stop afterwards
        risk = pos.entry_price - pos.initial_stop
        for name, level in (("breakeven move in one request", min(pos.entry_price, price * 0.990)),
                            ("trailing move in one request", min(pos.entry_price + 0.3 * risk, price * 0.994))):
            now_price = client.fetch_last_price(symbol)
            level = min(level, now_price * 0.995)
            if level - float(pos.protection.get("stop", pos.initial_stop)) < 0.11 * risk:
                level = float(pos.protection.get("stop", pos.initial_stop)) + 0.11 * risk
            n = len(rec.requests)
            old = [pos.sl_order_id, pos.protection.get("tp_order_id")]
            broker.move_stop(pos, level)
            pos.breakeven_moved = True
            broker.sync_stop(pos, clock())
            made = rec.requests[n:]
            olds = [rec.fetch_order(i, symbol) for i in old if i]
            opens = [o for o in rec.fetch_open_orders(symbol) if str(o.get("clientOrderId") or "").startswith("tb")]
            new = rec.fetch_order(pos.sl_order_id, symbol) if pos.sl_order_id else {}
            ok = (made == ["privatePostOrderCancelReplace"] and all(o.get("status") == "canceled" for o in olds)
                  and len(opens) == 1 and str(opens[0].get("id")) == str(pos.sl_order_id)
                  and abs(float(new.get("triggerPrice") or 0) - level) <= 0.002 * level)
            step(name, ok, f"requests {made}; replaced {[o.get('status') for o in olds]}; open bot orders now "
                 f"{len(opens)}, stop {new.get('triggerPrice')} (wanted {level:,.2f})")

        # 5. a move the price is already through
        through = client.fetch_last_price(symbol) * 1.01
        broker.move_stop(pos, through)
        broker.sync_stop(pos, clock())
        refused = pos.closing_reason
        if refused:
            broker.close_position(pos, client.fetch_last_price(symbol), refused, clock())
        else:  # not refused: put things back and sell
            broker.close_position(pos, client.fetch_last_price(symbol), "verify", clock())
        step("a move through the price sells at once", bool(refused) and pos.status == "closed",
             f"exchange refused the new stop after the cancel -> {refused!r}; position {pos.status} at "
             f"{pos.exit_price}")

        # 6. the exchange exits while the bot isn't looking
        price = client.fetch_last_price(symbol)
        pos2 = _position(symbol, price, budget / price, price * 0.9995, price * 1.0005, clock())
        broker.open_position(pos2, price, clock())
        broker.protect(pos2, clock())
        deadline = clock() + wait_s * 1000
        while pos2.status == "open" and not pos2.closing_reason and clock() < deadline:
            wait(poll_s)
            broker.sync([pos2], clock())
        if pos2.closing_reason:
            broker.close_position(pos2, client.fetch_last_price(symbol), pos2.closing_reason, clock())
        if pos2.status == "closed":
            step("an exchange-side exit while the bot isn't looking", pos2.exit_reason in ("take_profit", "stop_loss"),
                 f"{pos2.exit_reason} at {pos2.exit_price:,.2f} (levels {pos2.initial_stop:,.2f} / {pos2.take_profit:,.2f})")
        else:
            legs = [rec.fetch_order(i, symbol) for i in (pos2.sl_order_id, pos2.protection.get("tp_order_id")) if i]
            unbooked = [o for o in legs if float(o.get("filled") or 0.0) > 0]
            broker.close_position(pos2, client.fetch_last_price(symbol), "verify", clock())
            if unbooked:
                step("an exchange-side exit while the bot isn't looking", False,
                     f"an exit order filled on the exchange but sync did not book it: {[o.get('id') for o in unbooked]}")
            else:
                step("an exchange-side exit while the bot isn't looking", None,
                     f"the testnet price stayed between {pos2.initial_stop:,.2f} and {pos2.take_profit:,.2f} for "
                     f"{wait_s / 60:.0f} minutes; sold")

        # 7. lost answers
        cid, sent = new_client_id("vq"), clock()
        bid = client.price_to_precision(symbol, client.fetch_last_price(symbol) * 0.6)
        rec.create_order(symbol, "limit", "buy", broker._amount(symbol, 10.0 / bid), bid, {"clientOrderId": cid})
        found = broker._lookup(symbol, cid, sent)
        if found:
            broker._cancel(str(found["id"]), symbol)
        never = broker._lookup(symbol, new_client_id("vq"), clock())
        step("lost answers are found by client id", bool(found) and never is None,
             f"sent order found: {bool(found)}; an id never sent: {'not found' if never is None else 'FOUND'}")

        # 8. the core sleeve
        tmp = Path(db_path) if db_path else None
        db = Database(tmp or ":memory:")
        core = CoreSleeve(db, CoreConfig(symbols=list(core_symbols), min_trade_usd=6.0), Costs(0.001, 0.0005), "testnet",
                          client, executor=broker)
        broker.reserved = core.reserved
        core.initialize(2 * budget)
        prices = {s: client.fetch_last_price(s) for s in core_symbols}
        before = rec.fetch_balance().get("total") or {}  # the core's own change: not the satellite's dust
        bought = core.rebalance(clock(), prices, {s: 1.0 for s in core_symbols})
        held = dict(core.holdings)
        now_bal = rec.fetch_balance().get("total") or {}
        coin = lambda b, s: float(b.get(client.market(s)["base"]) or 0)  # noqa: E731
        match = all(abs(coin(now_bal, s) - coin(before, s) - q) <= max(1e-6, 0.01 * q) for s, q in held.items())
        sold = core.rebalance(clock(), prices, {s: 0.0 for s in core_symbols})
        cid, sent = new_client_id("vc"), clock()
        s0 = core_symbols[0]
        qty = broker._amount(s0, 6.0 / prices[s0] * 1.2)
        core._set("pending", {"client_id": cid, "symbol": s0, "side": "buy", "qty": qty, "sent_ms": sent, "reason": "verify"})
        rec.create_order(s0, "market", "buy", qty, None, {"clientOrderId": cid})  # the answer "lost"
        resolved = core.resolve_pending(clock())
        core.rebalance(clock(), prices, {s: 0.0 for s in core_symbols})
        ok = (len(bought) == len(core_symbols) and match and {t.side for t in sold} == {"sell"}
              and resolved is not None and core.pending is None)
        step("core orders", ok, f"bought {[f'{t.qty:g} {t.symbol}' for t in bought]}, exchange balances "
             f"{'match' if match else 'DO NOT match'} the ledger; sold {[f'{t.qty:g} {t.symbol}' for t in sold]}; "
             f"a lost core order {'was resolved' if resolved else 'was NOT resolved'}; left {core.holdings}")
    except Exception as exc:  # noqa: BLE001 - recorded, then cleaned up
        step("run", False, f"stopped by {type(exc).__name__}: {exc}")
    finally:
        steps.append(_clean_up(client, rec, {symbol, *core_symbols}, start_base, log))
    return steps


def _clean_up(client, rec, symbols, start_base: dict, log) -> Step:
    """Cancel the run's own orders still open, sell what the run bought; report any dust left."""
    left, dust = [], []
    for s in sorted(symbols):
        try:
            for o in rec.fetch_open_orders(s):
                if str(o.get("clientOrderId") or "") in rec.client_ids:
                    try:
                        rec.cancel_order(str(o["id"]), s)
                    except Exception:  # noqa: BLE001 - recorded below
                        left.append(f"{s} {o['id']}")
            total = float((rec.fetch_balance().get("free") or {}).get(client.market(s)["base"]) or 0.0)
            leftover = total - start_base.get(s, total)
            try:  # ccxt refuses to round an amount under one lot step: that is dust
                extra = client.amount_to_precision(s, leftover) if leftover > 0 else 0.0
            except Exception:  # noqa: BLE001
                extra = 0.0
            if extra * client.fetch_last_price(s) >= 6.0:
                rec.create_order(s, "market", "sell", extra, None, {"clientOrderId": f"tbvCLEAN{s[:3]}{int(time.time()) % 10_000}"})
            elif leftover > 1e-12:
                dust.append(f"{leftover:.8g} {client.market(s)['base']}")
        except Exception as exc:  # noqa: BLE001
            left.append(f"{s}: {exc}")
    detail = ("the run's orders cancelled, its coins sold" + (f" (dust under the minimum order left: {', '.join(dust)})"
                                                              if dust else "")) if not left else "; ".join(left)
    step = Step("clean-up", not left, detail)
    log(f"  [{'ok' if step.ok else 'FAILED'}] clean-up: {step.detail}")
    return step


def save(steps: list[Step], state_dir, code: str) -> Path:
    folder = Path(state_dir) / "testnet"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M", time.gmtime())
    path = folder / f"verify-{stamp}.json"
    path.write_text(json.dumps({"at": stamp, "code": code, "passed": all(s.ok is not False for s in steps),
                                "steps": [asdict(s) for s in steps]}, indent=1))
    return path
