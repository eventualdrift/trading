import json
import re
import urllib.error
import urllib.request

import pytest

from tradebot.backtest.engine import Costs
from tradebot.backtest.selection import Selection
from tradebot.bot import TradingBot
from tradebot.dashboard import build_data, render, serve_dashboard, strip_html, write_dashboard
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.notify import MemoryNotifier

DAY = 86_400_000


def payload(page: str) -> dict:
    m = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
    return json.loads(m.group(1))


@pytest.fixture
def bot_setup(cfg):
    cfg.core.fraction = 0.65
    market = SyntheticMarket(["BTC/USDT", "ETH/USDT"], days=300, base_tf="1h", seed=21)
    db = Database(cfg.state_path / "tradebot.db")
    broker = PaperBroker(db, Costs(cfg.costs.fee_rate, cfg.costs.slippage_rate), 1000.0, market)
    notes = MemoryNotifier()
    bot = TradingBot(cfg, market, broker, db, notes, selection=Selection(0.0, []))
    return market, db, notes, bot


def run(market, bot, days, start_day=200):
    for d in range(start_day, start_day + days):
        for h in (0, 6, 12, 18):
            now = market.start_ms + d * DAY + h * 3_600_000 + 30_000
            market.set_now(now)
            bot.tick(now)


def test_dashboard_has_sleeves_btc_and_events(bot_setup, cfg):
    market, db, notes, bot = bot_setup
    run(market, bot, 20)
    data = build_data(db, cfg)
    s = data["series"]
    assert len(s["t"]) >= 20 and len(s["total"]) == len(s["t"]) == len(s["satellite"])
    assert s["core"] is not None and len(s["core"]) == len(s["t"])
    assert s["btc"][0] == pytest.approx(s["total"][0])  # BTC hold starts at the account's value
    assert all(t2 > t1 for t1, t2 in zip(s["t"], s["t"][1:]))
    assert data["core"]["enabled"] and data["state"]["label"] == "Running"
    assert any("Core sleeve started" in e["text"] for e in data["events"])
    assert not any("<b>" in e["text"] for e in data["events"])  # stored as plain text
    page = render(data)
    assert payload(page)["series"]["t"] == s["t"]


def test_bot_writes_the_dashboard_file(bot_setup, cfg):
    market, db, notes, bot = bot_setup
    run(market, bot, 2)
    path = cfg.state_path / "dashboard.html"
    assert path.exists()
    data = payload(path.read_text())
    assert data["mode"] == "paper" and data["series"]["t"]
    assert not list(cfg.state_path.glob("*.tmp"))  # written atomically


def test_render_cannot_be_broken_out_of(cfg, tmp_path):
    db = Database(tmp_path / "x.db")
    evil = "</script><script>alert(1)</script><!-- & 'quotes' \"too\""
    db.log_event(1, "paper", evil)
    cfg.name = "<b>B</b>"
    page = render(build_data(db, cfg))
    assert page.count("</script>") == 2  # only the page's own two script blocks
    assert "<title>tradebot · &lt;b&gt;B&lt;/b&gt;</title>" in page
    assert payload(page)["events"][0]["text"] == evil


def test_name_prefix_and_events(bot_setup, cfg):
    market, db, notes, bot = bot_setup
    cfg.name = "B"
    bot.notify("🟢 <b>Opened</b> BTC/USDT &amp; more")
    assert notes.messages[-1].startswith("[B] ")
    assert db.events("paper", 1)[0]["text"] == "[B] 🟢 Opened BTC/USDT & more"
    assert strip_html("a <i>b</i> &lt;c&gt;") == "a b <c>"


def test_serve_dashboard_localhost(bot_setup, cfg):
    market, db, notes, bot = bot_setup
    run(market, bot, 1)
    server = serve_dashboard(db, cfg, 0, background=True)  # port 0: any free port
    try:
        host, port = server.server_address[:2]
        assert host == "127.0.0.1"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            assert r.status == 200 and r.headers["Cache-Control"] == "no-store"
            assert payload(r.read().decode())["series"]["t"]
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../tradebot.db", timeout=10)
        assert err.value.code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_open_pnl_uses_last_known_prices(bot_setup, cfg):
    from tradebot.models import Position

    market, db, notes, bot = bot_setup
    pos = Position(symbol="BTC/USDT", timeframe="1h", strategy="trend", side="long", mode="paper", amount=2.0,
                   entry_price=100.0, stop_loss=90.0, take_profit=130.0, initial_stop=90.0, opened_at=1,
                   max_hold_until=10**13)
    db.insert_position(pos)
    db.kv_set("paper:last_prices", {"BTC/USDT": 110.0})
    row = build_data(db, cfg)["positions"][0]
    assert row["upnl"] == pytest.approx(20.0) and row["r"] == pytest.approx(1.0)


def test_second_instance_can_leave_telegram_commands_to_the_first(bot_setup, cfg):
    market, db, notes, bot = bot_setup

    class Listening(MemoryNotifier):
        started = 0

        def start_listener(self, handler):
            Listening.started += 1

    bot.notifier = Listening()
    cfg.telegram.commands = False
    bot.stop()  # run_forever returns straight after start-up
    bot.run_forever()
    assert Listening.started == 0
    cfg.telegram.commands = True
    bot.run_forever()
    assert Listening.started == 1


def test_dashboard_cli_writes_file(tmp_path, monkeypatch):
    from tradebot.cli import main

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    conf = tmp_path / "b.yaml"
    conf.write_text(f"name: B\nstate_dir: {tmp_path / 'state-b'}\ndashboard:\n  port: 8766\n")
    out = tmp_path / "d.html"
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "dashboard", "--out", str(out)])
    data = payload(out.read_text())
    assert data["title"] == "tradebot · B" and data["series"]["t"] == []
