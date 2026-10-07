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


def test_rows_written_by_a_newer_version_can_still_be_read(tmp_path):
    """Old code must not crash on a column added by newer code (a pull without a restart)."""
    from tradebot.db import Database
    from tradebot.models import Position

    db = Database(tmp_path / "t.db")
    pid = db.insert_position(Position(symbol="A/USDT", timeframe="1d", strategy="x", side="long", mode="paper",
                                      amount=1, entry_price=10, stop_loss=9, take_profit=12, initial_stop=9,
                                      opened_at=0, max_hold_until=1))
    db._conn.execute("ALTER TABLE positions ADD COLUMN some_future_field REAL DEFAULT 1.5")
    assert db.get_position(pid).symbol == "A/USDT"


def test_bot_activity_log(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.last_botlog("paper", "universe") is None
    db.log_bot(200, "paper", "universe", ["BTC/USDT", "ETH/USDT"])
    db.log_bot(100, "paper", "universe", ["BTC/USDT"])
    db.log_bot(150, "live", "universe", ["SOL/USDT"])
    db.log_bot(300, "paper", "core_day", {"day": 0, "targets": {"BTC/USDT": 0.75}})
    assert db.botlog("paper", "universe") == [(100, ["BTC/USDT"]), (200, ["BTC/USDT", "ETH/USDT"])]
    assert db.botlog("paper", "universe", since_ms=150) == [(200, ["BTC/USDT", "ETH/USDT"])]
    assert db.last_botlog("paper", "universe") == (200, ["BTC/USDT", "ETH/USDT"])
    assert db.botlog("paper", "core_day")[0][1]["targets"] == {"BTC/USDT": 0.75}


def test_an_observed_bots_database_is_opened_read_only(tmp_path):
    import sqlite3

    import pytest

    bot = Database(tmp_path / "bot.db")
    bot.insert_signal(Signal("A/USDT", "1h", "trend", "long", 1, 0.9, 1.2, 0, 0, 0, 0))
    bot.log_bot(1, "paper", "universe", ["A/USDT"])
    seen = Database(tmp_path / "bot.db", readonly=True)
    assert seen.recent_signals(1)[0].symbol == "A/USDT" and seen.botlog("paper", "universe") == [(1, ["A/USDT"])]
    with pytest.raises(sqlite3.OperationalError):
        seen.log_bot(2, "paper", "universe", ["B/USDT"])  # it can't write to the bot's state
    with pytest.raises(FileNotFoundError):
        Database(tmp_path / "missing.db", readonly=True)  # nor create one
    assert not (tmp_path / "missing.db").exists()
