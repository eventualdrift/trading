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
