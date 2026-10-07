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
from tradebot.reconcile import (Reconciliation, Timeline, format_reconciliation, format_uptime, reconcile,
                                uptime, write_csv)
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
    # the account replay takes from every signal and frees a slot when the position is flat, like
    # paper: with one contested slot, every taken/skipped decision agrees
    assert not [r for r in rows if r.cause]
    assert sum(r.backtest == "taken" for r in rows) >= 10 and any("max open positions" in r.paper for r in rows)
    text = format_reconciliation(rec, cfg)
    assert "the bot was running 100% of the window" in text and "Down per UTC day: none" in text
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
    by_candle = {}
    for s in sigs:
        by_candle.setdefault(s.candle_time, []).append(s)
    lone = [s for s in sigs if s.status == "skipped" and len(by_candle[s.candle_time]) == 1]  # alone at its close
    assert len(opened) >= 2 and len(lone) >= 5
    # 1) paper paid 1% more on one entry
    pos = positions[opened[0].id]
    pos.entry_price *= 1.01
    db2.update_position(pos)
    # 2) paper had no signal although it scanned; 3) a scan error; 4) the bot was down; 5) the candle
    # was skipped as stale; 6) the bot was up but no scan was recorded
    gone, errored, down, stale, unscanned = lone[:5]
    for s in (gone, errored, down, stale, unscanned):
        db2._conn.execute("DELETE FROM signals WHERE id=?", (s.id,))
    for s in (down, stale, unscanned):
        db2._conn.execute("DELETE FROM botlog WHERE kind='scan' AND data LIKE ?", (f'%"candle": {s.candle_time},%',))
    db2.log_bot(errored.created_at, "paper", "scan_errors",
                {"tf": "1h", "candle": errored.candle_time, "errors": [f"{errored.symbol} 1h: latest candle missing/stale"]})
    db2.log_bot(stale.created_at, "paper", "scan_skipped", {"tf": "1h", "candle": stale.candle_time, "age_s": 5400})
    close = down.candle_time + HOUR
    db2._conn.execute("DELETE FROM snapshots WHERE ts BETWEEN ? AND ?", (close - 10 * 60_000, close + 2 * HOUR))
    # 7) a live-only check stopped paper from opening a trade the backtest took
    first = opened[1]
    db2._conn.execute("UPDATE signals SET status='skipped', note=? WHERE id=?",
                      ("price 1.0 already outside the entry zone", first.id))
    db2._conn.execute("DELETE FROM positions WHERE signal_id=?", (first.id,))

    rec = run_reconcile(paper_run, db2)
    rows = {(r.symbol, r.candle_ms): r for r in rec.rows}
    assert rows[(opened[0].symbol, opened[0].candle_time)].entry_bps == pytest.approx(100, abs=15)
    assert rows[(gone.symbol, gone.candle_time)].paper == "(no signal: scanned, live found no signal (its candles differ))"
    assert rows[(errored.symbol, errored.candle_time)].paper.startswith("(no signal: scan error: ")
    assert rows[(down.symbol, down.candle_time)].paper.startswith("(no signal: bot not running at the close")
    assert rows[(stale.symbol, stale.candle_time)].paper == \
        "(no signal: candle skipped: the bot reached it 1h30 after the close (stale-candle rule))"
    assert rows[(unscanned.symbol, unscanned.candle_time)].paper == "(no signal: no scan recorded while the bot was running)"
    row = rows[(first.symbol, first.candle_time)]
    assert row.backtest == "taken" and row.cause == "first difference: a live-only check (price 1.0 already outside the entry zone)"
    others = [r for r in rec.rows if r.cause and r is not row]
    assert all(r.cause.startswith("knock-on: holdings already differed") for r in others)
    text = format_reconciliation(rec, cfg)
    assert "Backtest only:" in text and "Every gap (1;" in text and "1 first differences" in text


def test_csv_has_every_row(paper_run, tmp_path):
    rec = run_reconcile(paper_run)
    write_csv(rec, tmp_path / "rec.csv")
    df = pd.read_csv(tmp_path / "rec.csv")
    assert len(df) == len(rec.rows) and {"paper", "backtest", "cause", "entry_bps", "exit_late_h"} <= set(df.columns)


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
    assert "the bot computed it from the candle of" in wrong[0]["why"]  # the logged close it used, beside the backtest's
    assert "paper no trade, backtest " + step["side"] in format_reconciliation(rec, cfg)


def test_timeline_and_uptime(cfg):
    tl = Timeline([(100, ["A"]), (200, ["A", "B"])], ["X"])
    assert tl.at(50) == ["X"] and tl.at(100) == ["A"] and tl.at(250) == ["A", "B"]
    assert tl.approximate(50) and not tl.approximate(150)
    db = Database(cfg.state_path / "u.db")
    q = 15 * 60_000
    for k in range(20):
        if not 8 <= k < 12:  # an hour without snapshots
            db.record_snapshot(k * q, "paper", 1000.0, 0.0, 1000.0, None)
    up = uptime(db, "paper")
    assert up.up(3 * q) and not up.up(10 * q)
    assert not up.up(7 * q + 10 * 60_000)  # 10 minutes after the last snapshot before the gap: already down
    assert [(g.start, g.end) for g in up.gaps] == [(7 * q, 12 * q)]
    assert up.gaps[0].cause == "before the start/stop/sleep log"


def test_gaps_are_explained_by_starts_stops_and_pauses(cfg):
    db = Database(cfg.state_path / "g.db")
    q, h, t0 = 15 * 60_000, HOUR, 2 * DAY  # from 1970-01-03 00:00 UTC
    alive = [(0, 2), (10, 12), (15, 17), (20, 22), (30, 32)]  # hours the bot ran
    for a, b in alive:
        for t in range(t0 + a * h, t0 + b * h + 1, q):
            db.record_snapshot(t, "paper", 1000.0, 0.0, 1000.0, None)
    db.log_bot(t0 - h, "paper", "start", {"prev_alive_ms": None, "prev_clean_stop": None})
    db.log_bot(t0 + 10 * h, "paper", "pause", {"from": t0 + 2 * h, "to": t0 + 10 * h, "gap_s": 8 * 3600,
                                               "asleep_s": 8 * 3600 - 60})
    db.log_bot(t0 + 15 * h, "paper", "pause", {"from": t0 + 12 * h, "to": t0 + 15 * h, "gap_s": 3 * 3600, "asleep_s": 0})
    db.log_bot(t0 + 17 * h + q, "paper", "stop", {"why": "SIGTERM: service stopped or restarted"})
    db.log_bot(t0 + 20 * h, "paper", "start", {"prev_alive_ms": t0 + 17 * h, "prev_clean_stop": True})
    db.log_bot(t0 + 30 * h, "paper", "start", {"prev_alive_ms": t0 + 22 * h, "prev_clean_stop": False})
    up = uptime(db, "paper", t0, t0 + 32 * h)
    causes = [g.cause for g in up.gaps]
    assert len(causes) == 4
    assert causes[0] == "computer asleep 7h59"
    assert causes[1] == "bot running but not looping 3h00 (a hang or a very slow call)"
    assert causes[2] == "bot stopped 01-03 17:15 (SIGTERM: service stopped or restarted); restarted 01-03 20:00"
    assert causes[3] == ("bot ended without a clean stop (crash, kill or power loss), last alive 01-03 22:00; "
                         "restarted 01-04 06:00")
    assert up.down_ms(t0, t0 + 32 * h) == (8 + 3 + 3 + 8) * h
    assert up.up(t0 + h) and not up.up(t0 + 2 * h + 10 * 60_000) and not up.up(t0 + 25 * h)
    rec = Reconciliation(t0, t0 + 32 * h, uptime=up, uptime_share=1 - up.down_ms(t0, t0 + 32 * h) / (32 * h))
    text = "\n".join(format_uptime(rec))
    assert "running 31% of the window" in text and "Starts, stops and sleep recorded" not in text
    assert "Down per UTC day: 01-03 16.0h of 24h, 01-04 6.0h of 8h" in text
    assert "Every gap (4;" in text and "01-03 02:00 -> 01-03 10:00   8h00  computer asleep 7h59" in text


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


def test_ml_filter_selections_in_force_and_paper_only_reasons(paper_run, tmp_path):
    cfg, market, db, data, *_ = paper_run
    db2 = copy_db(db, tmp_path / "ml.db")
    base = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}
    sigs = sorted(db2.signals_since(0), key=lambda s: s.candle_time)
    skipped = [s for s in sigs if s.status == "skipped" and base[(s.symbol, s.candle_time)].r_if_taken is not None]
    opened = [s for s in sigs if s.status == "opened"]
    assert len(skipped) >= 2 and opened
    # the filter switched on mid-window: two signals filtered, one trade sized up
    db2.log_bot(skipped[0].created_at - 60_000, "paper", "selection",
                {"combos": [{"strategy": "breakout", "timeframe": "1h", "params": {}}], "created_at": 1.7e9,
                 "ml": True, "ml_threshold": 0.55, "ml_trained_until": "2024-03-01T00:00:00"})
    for s in skipped[:2]:
        db2._conn.execute("UPDATE signals SET status='filtered', note='ML probability 40% < 55%', confidence=0.4 "
                          "WHERE id=?", (s.id,))
    db2._conn.execute("UPDATE signals SET confidence=0.9, risk_multiplier=1.4 WHERE id=?", (opened[-1].id,))
    # a paper signal on a candle where the backtest's entry condition is false
    quiet = next(t for t in data["1h"]["BTC/USDT"].index[-100:]
                 if ("BTC/USDT", int(t.value // 1_000_000)) not in base)
    from tradebot.models import Signal

    db2.insert_signal(Signal("BTC/USDT", "1h", "breakout", "long", 100.0, 95.0, 110.0, int(quiet.value // 1_000_000),
                             int(quiet.value // 1_000_000) + HOUR + 30_000, 0, 0, status="skipped", note="test"))
    rec = run_reconcile(paper_run, db2)
    assert rec.ml_active and [x["ml"] for x in rec.selections] == [False, True]
    text = format_reconciliation(rec, cfg)
    assert "ML filter ON (threshold 55%, trained to 2024-03-01)" in text
    assert "filtered out 2: as backtest trades they would have made: backtest trades" in text
    assert "sized up by confidence: 1 signals, risk x1.40" in text
    row = next(r for r in rec.rows if r.candle_ms == int(quiet.value // 1_000_000) and r.symbol == "BTC/USDT")
    assert row.backtest == ("(no signal: the entry condition is false there on the full history "
                            "(live used its last 1000 candles at most))")
    from tradebot.weekly import selection_changes

    changes = selection_changes(rec)
    assert len(changes) == 1 and "ML filter switched on" in changes[0]


def test_a_core_target_missing_at_the_bots_check_is_explained(core_run, tmp_path):
    cfg, db, closes, since, end = core_run
    db2 = copy_db(db, tmp_path / "core-none.db")
    ts, logged = db2.botlog("paper", "core_day")[7]
    logged["targets"] = {k: None for k in logged["targets"]}  # the fetch failed when the bot checked
    db2._conn.execute("UPDATE botlog SET data=? WHERE kind='core_day' AND ts=?", (json.dumps(logged), ts))
    rec = reconcile(db2, cfg, {}, None, closes, since, end, None, [])
    missing = [d for d in rec.core_days if d["day"] == int(logged["day"])]
    assert len(missing) == 2 and all("no daily candles at its check" in d["why"] for d in missing)
    assert "no daily candles at its check" in format_reconciliation(rec, cfg)
