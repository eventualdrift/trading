from tradebot.models import Position, Signal
from tradebot.notify import formatting as fmt
from tradebot.notify.telegram import TelegramNotifier


class Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class FakeSession:
    def __init__(self, updates=()):
        self.posts = []
        self.updates = list(updates)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/getUpdates"):
            return Resp({"ok": True, "result": self.updates})
        return Resp({"ok": True, "result": {}})


def upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_send_uses_html_and_chat():
    s = FakeSession()
    TelegramNotifier("TOKEN", 42, session=s).send("<b>hi</b>")
    url, payload = s.posts[0]
    assert url.endswith("/botTOKEN/sendMessage")
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"


def test_commands_only_from_owner():
    s = FakeSession([upd(1, 42, "/status"), upd(2, 999, "/closeall"), upd(3, 42, "hello"),
                     upd(4, 42, "/close@mybot 7")])
    calls = []
    tg = TelegramNotifier("T", "42", session=s)
    tg.poll_once(lambda c, a: calls.append((c, a)) or f"ok {c}")
    assert calls == [("status", []), ("close", ["7"])]
    replies = [p["text"] for u, p in s.posts if u.endswith("sendMessage")]
    assert replies == ["ok status", "ok close"]
    assert tg._offset == 5


def test_signal_message_contains_levels():
    sig = Signal("SOL/USDT", "4h", "trend", "long", 142.35, 136.80, 153.45, 0, 0, 0, 3_600_000,
                 reason="uptrend (EMA50 > EMA200)", confidence=0.64)
    pos = Position("SOL/USDT", "4h", "trend", "long", "paper", 1.8, 142.35, 136.80, 153.45, 136.80, 0, 0, id=12)
    text = fmt.format_signal(sig, pos, 1000.0, "paper", "USDT", 0.3, True)
    for piece in ("BUY", "SOL/USDT", "142.350", "136.800", "153.450", "1:2.0", "64%", "EMA50 &gt; EMA200", "#12"):
        assert piece in text
    pos.exit_price, pos.exit_reason, pos.pnl, pos.r_multiple = 153.45, "take_profit", 19.4, 2.0
    out = fmt.format_exit(pos, "USDT")
    assert "SELL NOW" in out and "Take-profit" in out and "+2.00R" in out


def test_fmt_price():
    assert fmt.fmt_price(65432.1) == "65,432.10"
    assert fmt.fmt_price(1.23456) == "1.2346"
    assert fmt.fmt_price(0.000123456) == "0.00012346"
