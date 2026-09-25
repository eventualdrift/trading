from tradebot.db import Database
from tradebot.models import Position, Signal


def test_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    s = Signal("ETH/USDT", "4h", "breakout", "long", 10.0, 9.0, 12.5, 1, 2, 3, 4,
               reason="why", confidence=0.61, features={"a": 1.5, "b": None})
    sid = db.insert_signal(s)
    s.status = "opened"
    db.update_signal(s)
    got = db.recent_signals(1)[0]
    assert got.id == sid and got.status == "opened" and got.features == {"a": 1.5, "b": None}
    p = Position("ETH/USDT", "4h", "breakout", "long", "paper", 2.0, 10.0, 9.0, 12.5, 9.0, 5, 99,
                 signal_id=sid, features={"x": 1.0})
    db.insert_position(p)
    p.breakeven_moved = True
    db.update_position(p)
    got = db.get_position(p.id)
    assert got.breakeven_moved is True and got.features == {"x": 1.0}
    assert [x.id for x in db.open_positions("paper")] == [p.id]
    assert db.open_positions("live") == []
    p.status, p.closed_at, p.r_multiple = "closed", 50, 1.2
    db.update_position(p)
    assert db.open_positions("paper") == [] and len(db.closed_positions("paper")) == 1
    assert len(db.trade_samples()) == 1


def test_kv_and_equity(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.kv_get("x", 5) == 5
    db.kv_set("x", {"a": [1, 2]})
    assert db.kv_get("x") == {"a": [1, 2]}
    db.record_equity(1000, "paper", 100.0)
    db.record_equity(2000, "paper", 90.0)
    assert list(db.equity_curve("paper")) == [100.0, 90.0]


def test_migration_adds_columns(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT)")
    con.commit()
    con.close()
    db = Database(path)
    db.insert_signal(Signal("A/USDT", "1h", "trend", "long", 1, 0.9, 1.2, 0, 0, 0, 0))
    assert db.recent_signals(1)[0].symbol == "A/USDT"
