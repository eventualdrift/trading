"""`tradebot testnet verify` against the fake exchange: every step passes, nothing is left open,
and it refuses anything that isn't the testnet."""
import json

import pytest

from tradebot import testnet as tn

from .fakespot import FakeSpotClient
from .test_brokers import Clock


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr("tradebot.data.exchange.time.sleep", lambda s: None)


def run(client, **kw):
    clock = Clock()

    def market_moves(seconds):  # the testnet price wanders up while step 6 waits
        clock.sleep(seconds)
        client.ex.set_price("BTC/USDT", client.ex.prices["BTC/USDT"] * 1.0002)

    return tn.verify(client, sleep=clock.sleep, clock=clock, wait=market_moves, log=lambda *_: None, **kw)


def test_every_step_passes_on_a_well_behaved_testnet(tmp_path):
    client = FakeSpotClient()
    steps = run(client)
    assert [s.name for s in steps] == ["account", "entry protected by an OCO pair", "breakeven move in one request",
                                       "trailing move in one request", "a move through the price sells at once",
                                       "an exchange-side exit while the bot isn't looking",
                                       "lost answers are found by client id", "core orders", "clean-up"]
    assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps if not s.ok]
    ex = client.ex
    assert not [o for o in ex.orders.values() if o["status"] == "open"]  # nothing left on the exchange
    assert ex.free["BTC"] == pytest.approx(0.0, abs=1e-5) and ex.free["ETH"] == pytest.approx(0.0, abs=1e-4)
    path = tn.save(steps, tmp_path, "abc1234")
    record = json.loads(path.read_text())
    assert record["passed"] and record["code"] == "abc1234" and len(record["steps"]) == 9


def test_a_flat_testnet_market_is_inconclusive_not_a_failure():
    client = FakeSpotClient()
    clock = Clock()
    steps = tn.verify(client, sleep=clock.sleep, clock=clock, wait=clock.sleep, log=lambda *_: None, wait_s=60)
    exit_step = next(s for s in steps if s.name.startswith("an exchange-side exit"))
    assert exit_step.ok is None and "stayed between" in exit_step.detail
    assert all(s.ok is not False for s in steps)


def test_it_refuses_the_real_exchange_before_sending_anything():
    client = FakeSpotClient(sandbox=False)
    with pytest.raises(RuntimeError, match="not on the testnet"):
        tn.verify(client, log=lambda *_: None)
    assert client.ex.calls == []


def test_too_little_testnet_money_stops_at_the_first_step():
    client = FakeSpotClient(quote_balance=20.0)
    steps = run(client)
    assert steps[0].name == "account" and steps[0].ok is False and steps[-1].name == "clean-up"
    assert [c for c in client.ex.calls if c[0] in ("create", "oco", "replace")] == []


def test_the_command_needs_testnet_keys(tmp_path, monkeypatch, capsys):
    from tradebot.cli import main

    for k in ("TESTNET_API_KEY", "TESTNET_API_SECRET", "TRADEBOT_MODE"):
        monkeypatch.delenv(k, raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\n")
    with pytest.raises(SystemExit, match="TESTNET_API_KEY"):
        main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "testnet", "verify"])


def test_a_testnet_account_s_own_coins_and_real_lot_steps_don_t_hide_an_exchange_exit(tmp_path):
    """The testnet funds every account with coins; Binance's BTC lot step leaves dust. An exit fill
    is checked against the coins there were once the protection stood, so sync still books it, and
    the core's check counts only its own change."""
    client = FakeSpotClient(balances={"BTC": 1.0, "ETH": 100.0}, lot=1e-5)
    steps = run(client)
    assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps if not s.ok]
    exit_step = next(s for s in steps if s.name.startswith("an exchange-side exit"))
    assert exit_step.ok is True and "take_profit" in exit_step.detail
    assert client.ex.free["BTC"] == pytest.approx(1.0, abs=2e-4) and client.ex.free["ETH"] == pytest.approx(100.0, abs=2e-3)


def test_it_won_t_run_beside_a_testnet_bot_on_the_same_keys():
    client = FakeSpotClient()
    client.ex.create_order("BTC/USDT", "market", "buy", 0.5, None, {"clientOrderId": "tbeBOT"})
    client.ex.create_order("BTC/USDT", "market", "sell", 0.4, None, {"stopLossPrice": 90.0, "clientOrderId": "tbsBOT"})
    steps = run(client)
    assert steps[0].ok is False and "testnet bot" in steps[0].detail
    assert [o["status"] for o in client.ex.orders.values() if o["clientOrderId"] == "tbsBOT"] == ["open"]


def test_clean_up_touches_only_the_run_s_own_orders():
    client = FakeSpotClient()
    rec = tn.Recorder(client.ex)
    client.ex = rec
    rec.create_order("BTC/USDT", "market", "buy", 1.0, None, {"clientOrderId": "tbeRUN"})
    mine = rec.create_order("BTC/USDT", "market", "sell", 0.5, None, {"stopLossPrice": 90.0, "clientOrderId": "tbsRUN"})
    rec._ex.create_order("BTC/USDT", "market", "buy", 1.0, None, {"clientOrderId": "tbeOTHER"})
    other = rec._ex.create_order("BTC/USDT", "market", "sell", 0.5, None, {"stopLossPrice": 90.0, "clientOrderId": "tbsOTHER"})
    step = tn._clean_up(client, rec, {"BTC/USDT"}, {"BTC/USDT": 0.999}, lambda *_: None)
    assert step.ok and rec._ex.orders[mine["id"]]["status"] == "canceled"
    assert rec._ex.orders[other["id"]]["status"] == "open"


def test_a_real_lot_step_and_price_leave_dust_that_is_reported_not_failed():
    client = FakeSpotClient({"BTC/USDT": 63_000.0, "ETH/USDT": 2_500.0}, balances={"BTC": 1.0, "ETH": 100.0}, lot=1e-5)
    clock = Clock()

    def market_moves(seconds):
        clock.sleep(seconds)
        client.ex.set_price("BTC/USDT", client.ex.prices["BTC/USDT"] * 1.0002)

    steps = tn.verify(client, sleep=clock.sleep, clock=clock, wait=market_moves, log=lambda *_: None)
    assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps if not s.ok]
    assert steps[-1].name == "clean-up"


def test_an_earlier_run_s_leftovers_are_cancelled_not_taken_for_a_bot():
    client = FakeSpotClient()
    client.ex.create_order("BTC/USDT", "market", "buy", 0.5, None, {"clientOrderId": "tbveOLD"})
    old = client.ex.create_order("BTC/USDT", "market", "sell", 0.4, None, {"stopLossPrice": 90.0, "clientOrderId": "tbvsOLD"})
    steps = run(client)
    assert steps[0].ok and client.ex.orders[old["id"]]["status"] == "canceled"
