import json
import sqlite3

import pandas as pd
import pytest

from tradebot.backtest.selection import ComboResult, Selection
from tradebot.bot import TradingBot
from tradebot.config import BotConfig
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.learning import load_context
from tradebot.notify import MemoryNotifier
from tradebot.reconcile import Timeline, format_reconciliation, reconcile, uptime, write_csv
from tradebot.timeframes import drop_unclosed

DAY = 86_400_000
HOUR = 3_600_000


def selection():
    return Selection(0.0, [ComboResult("breakout", "1h", {}, {}, {}, 1, 1.0, True)])


@pytest.fixture(scope="module")
def paper_run(tmp_path_factory):
    """A paper bot on synthetic candles for two weeks with one slot, so slots are contested."""
    tmp = tmp_path_factory.mktemp("reconcile")
    cfg = BotConfig(state_dir=str(tmp / "state"))
    cfg.data.dir = str(tmp / "data")
    cfg.risk.max_open_positions = 1
    market = SyntheticMarket(3, days=100, seed=9, base_tf="1h")
    start = (market.now_ms() - 16 * DAY) // HOUR * HOUR
    market.set_now(start + 30_000)
    db = Database(cfg.state_path / "tradebot.db")
    bot = TradingBot(cfg, market, PaperBroker(db, cfg.costs_model(), 1000.0, market), db, MemoryNotifier(),
                     selection=selection())
    t = start + HOUR
    while t < start + 14 * DAY:
        market.set_now(t + 30_000)
        bot.tick(t + 30_000)
        t += HOUR
    data = {"1h": {s: drop_unclosed(market.history(s, "1h", t - 150 * DAY, t), "1h", t) for s in market.symbols}}
    ctx = load_context(market, cfg, None, end_ms=t, log_fn=lambda *_: None)
    return cfg, market, db, data, ctx, start + HOUR + 30_000, t  # since = the first tick


def run_reconcile(paper_run, db=None):
    cfg, market, db0, data, ctx, since, end = paper_run
    return reconcile(db or db0, cfg, data, ctx, {}, since, end, selection(), market.symbols)


def copy_db(db: Database, path) -> Database:
    dst = sqlite3.connect(path)
    db._conn.backup(dst)
    dst.close()
    return Database(path)


def test_reconciliation_of_a_paper_run_matches_trade_by_trade(paper_run):
    cfg = paper_run[0]
    rec = run_reconcile(paper_run)
    rows = rec.rows
    assert len(rows) >= 20
    # the bot ran the whole time with everything logged: every signal is on both sides
    assert rec.uptime_share == 1.0
    assert not [r for r in rows if r.paper.startswith("(no signal")]
    assert not [r for r in rows if r.backtest == "(no signal)"]
    fills = [r for r in rows if r.entry_bps is not None]
    assert len(fills) >= 5
    assert all(abs(r.entry_bps) < 15 for r in fills)  # paper's market entries sit on the modelled fill
    closed = [r for r in fills if r.exit_paper not in (None, "still open")]
    assert closed
    assert all(r.exit_paper.split(" @")[0] == r.exit_model.split(" @")[0] for r in closed)
    assert all(r.exit_same_candle for r in closed)
    assert all(abs(r.fees_paper_pct - r.fees_model_pct) < 0.02 for r in closed)
    # slot disagreements the backtest's early slot release explains: paper was full at that moment
    early = [r for r in rows if r.backtest == "taken" and r.backtest_exits_at_close != "taken"
             and not r.paper.startswith("opened")]
    assert all("max open positions" in r.paper for r in early)
    text = format_reconciliation(rec, cfg)
    assert "Bot running 100% of the window" in text
    assert "not logged" not in text  # coin list and strategies were logged from the first tick
    assert "Taken vs skipped agree on" in text and "Core:\n  off in this configuration" in text


def test_reconciliation_names_what_differs(paper_run, tmp_path):
    cfg, market, db, *_ = paper_run
    db2 = copy_db(db, tmp_path / "copy.db")
    base = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}
    positions = db2.positions_by_signal("paper")
    sigs = sorted(db2.signals_since(0), key=lambda s: s.candle_time)
    opened = [s for s in sigs if s.id in positions and positions[s.id].status == "closed"
              and base[(s.symbol, s.candle_time)].entry_bps is not None]
    skipped = [s for s in sigs if s.status == "skipped"]
    assert opened and len(skipped) >= 3
    # 1) paper paid 1% more on one entry
    pos = positions[opened[0].id]
    pos.entry_price *= 1.01
    db2.update_position(pos)
    # 2) a signal paper never recorded; 3) one lost to a scan error; 4) one while the bot was down
    gone, errored, down = skipped[0], skipped[1], skipped[2]
    for s in (gone, errored, down):
        db2._conn.execute("DELETE FROM signals WHERE id=?", (s.id,))
    db2.log_bot(errored.created_at, "paper", "scan_errors",
                {"tf": "1h", "candle": errored.candle_time, "errors": [f"{errored.symbol} 1h: latest candle missing/stale"]})
    close = down.candle_time + HOUR
    db2._conn.execute("DELETE FROM snapshots WHERE ts BETWEEN ? AND ?", (close - 2 * HOUR, close + 2 * HOUR))

    rec = run_reconcile(paper_run, db2)
    rows = {(r.symbol, r.candle_ms): r for r in rec.rows}
    assert rows[(opened[0].symbol, opened[0].candle_time)].entry_bps == pytest.approx(100, abs=15)
    assert rows[(gone.symbol, gone.candle_time)].paper == "(no signal: not explained)"
    assert rows[(errored.symbol, errored.candle_time)].paper.startswith("(no signal: scan error: ")
    assert rows[(down.symbol, down.candle_time)].paper == "(no signal: bot not running)"
    text = format_reconciliation(rec, cfg)
    assert "Backtest only: " in text and "bot not running" in text and "down " in text


def test_csv_has_every_row(paper_run, tmp_path):
    rec = run_reconcile(paper_run)
    write_csv(rec, tmp_path / "rec.csv")
    df = pd.read_csv(tmp_path / "rec.csv")
    assert len(df) == len(rec.rows) and {"paper", "backtest", "entry_bps", "backtest_exits_at_close"} <= set(df.columns)


@pytest.fixture(scope="module")
def core_run(tmp_path_factory):
    """The core on paper for 80 days, with the bot down for three day changes."""
    tmp = tmp_path_factory.mktemp("core")
    cfg = BotConfig(state_dir=str(tmp / "state"))
    cfg.core.fraction = 0.65
    market = SyntheticMarket(["BTC/USDT", "ETH/USDT"], days=300, base_tf="1h", seed=21)
    db = Database(cfg.state_path / "core.db")
    market.set_now(market.start_ms + 195 * DAY)
    bot = TradingBot(cfg, market, PaperBroker(db, cfg.costs_model(), 1000.0, market), db, MemoryNotifier(),
                     selection=Selection(0.0, []))
    for d in range(195, 275):
        if 240 <= d < 243:
            continue
        now = market.start_ms + d * DAY + 30_000
        market.set_now(now)
        bot.tick(now)
    end = market.start_ms + 275 * DAY
    closes = {s: market.history(s, "1d", market.start_ms, end)["close"] for s in cfg.core.symbols}
    return cfg, db, closes, market.start_ms + 195 * DAY, end


def test_core_targets_and_rebalances_match_the_backtest(core_run):
    cfg, db, closes, since, end = core_run
    rec = reconcile(db, cfg, {}, None, closes, since, end, None, [])
    assert rec.core_rule_days >= 70 and rec.core_rule_diffs == []
    days = [d for d in rec.core_days if d["paper"] is not None]
    assert days and all(d["paper"] == pytest.approx(d["backtest"]) for d in days)
    assert len(rec.core_days_missing) == 3  # the three day changes the bot missed
    trades = [t for t in rec.core_trades if t["reason"] == "trend weights"]
    assert trades and all(abs(t["bps"]) < 1 for t in trades)  # fills at the close +/- slippage, like the backtest
    text = format_reconciliation(rec, cfg)
    assert "coin-days match the backtest" in text and "0 coin-days trade differently" in text
    assert "3 days with no core check" in text


def test_core_reconciliation_flags_a_wrong_target_and_a_missing_trade(core_run, tmp_path):
    cfg, db, closes, since, end = core_run
    db2 = copy_db(db, tmp_path / "core-copy.db")
    step = next(t for t in db2.core_trades("paper", 10_000)[::-1]
                if t["reason"] == "trend weights" and t["weight_from"] != t["weight_to"])
    db2._conn.execute("DELETE FROM core_trades WHERE ts=? AND symbol=?", (step["ts"], step["symbol"]))
    ts, logged = db2.botlog("paper", "core_day")[5]
    logged["targets"]["BTC/USDT"] = 0.0 if logged["targets"]["BTC/USDT"] else 1.0
    db2._conn.execute("UPDATE botlog SET data=? WHERE kind='core_day' AND ts=?", (json.dumps(logged), ts))
    rec = reconcile(db2, cfg, {}, None, closes, since, end, None, [])
    assert any(m["symbol"] == step["symbol"] and m["paper"] is None and m["backtest"] == step["side"]
               for m in rec.core_rule_diffs)
    wrong = [d for d in rec.core_days if d["paper"] is not None and d["paper"] != d["backtest"]]
    assert [(d["day"], d["symbol"]) for d in wrong] == [(int(logged["day"]), "BTC/USDT")]
    assert "paper no trade, backtest " + step["side"] in format_reconciliation(rec, cfg)


def test_timeline_and_uptime(cfg):
    tl = Timeline([(100, ["A"]), (200, ["A", "B"])], ["X"])
    assert tl.at(50) == ["X"] and tl.at(100) == ["A"] and tl.at(250) == ["A", "B"]
    assert tl.approximate(50) and not tl.approximate(150)
    db = Database(cfg.state_path / "u.db")
    for k in range(20):
        if not 8 <= k < 12:  # an hour without snapshots
            db.record_snapshot(k * 15 * 60_000, "paper", 1000.0, 0.0, 1000.0, None)
    up, downs, first, last = uptime(db, "paper")
    assert up(3 * 15 * 60_000) and not up(10 * 15 * 60_000)
    assert downs == [(7 * 15 * 60_000, 12 * 15 * 60_000)]


def test_cli_reconcile_runs_on_frozen_candles(tmp_path, monkeypatch, capsys):
    from tradebot.cli import main
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 4\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 600}\n")
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "reconcile", "--synthetic",
          "--since", "2025-03-01", "--end", "2025-06-01", "--csv", str(tmp_path / "r.csv")])
    out = capsys.readouterr().out
    assert "Paper vs backtest reconciliation, 2025-03-01 00:00 to 2025-06-01 00:00 UTC (paper)" in out
    assert 'Reproduce: tradebot reconcile --since "2025-03-01 00:00" --end "2025-06-01 00:00"' in out
    assert "Candles frozen at 2025-06-01 00:00 UTC" in out
    assert (tmp_path / "r.csv").exists()
