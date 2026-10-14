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
                                selection_timeline, uptime, write_csv)
from tradebot.timeframes import drop_unclosed

from .conftest import bars

DAY = 86_400_000
HOUR = 3_600_000


def selection():
    return Selection(0.0, [ComboResult("breakout", "1h", {}, {}, {}, 1, 1.0, True)])


@pytest.fixture(autouse=True)
def hourly_bot_sees_targets_at_closes(monkeypatch):
    """The paper bots here run on hourly synthetic prices, so they see a target only at a candle's
    close; the replay they are reconciled with counts it the same way. (The real bot polls every
    30 s, which the backtest's default touch rule models, a spike shorter than a poll aside.)"""
    import inspect

    from tradebot.backtest import engine

    real = engine.simulate_trade

    def on_close(*args, **kwargs):
        bound = inspect.signature(real).bind(*args, **kwargs)
        bound.arguments["target_on_close"] = True
        return real(*bound.args, **bound.kwargs)

    monkeypatch.setattr(engine, "simulate_trade", on_close)


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
    assert rows[(gone.symbol, gone.candle_time)].paper == ("(no signal: scanned, live found no signal (true on the last "
                                                           "999 candles, as the scan had them, too: its candles differ))")
    assert rows[(gone.symbol, gone.candle_time)].candle_window == "doesn't explain"
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
                            "(false on the last 999 candles the scan had too: not live's candle window))")
    assert row.candle_window == "doesn't explain"
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


def test_a_core_day_that_waited_for_data_is_explained(core_run, tmp_path):
    cfg, db, closes, since, end = core_run
    db2 = copy_db(db, tmp_path / "core-wait.db")
    days = db2.botlog("paper", "core_day")
    ts, logged = days[6]  # waited two hours for the candle, then rebalanced on it (same targets as the backtest)
    day = int(logged["day"])
    db2.log_bot(day + DAY + 60_000, "paper", "core_retry", {"day": day, "missing": ["BTC/USDT daily close"]})
    db2._conn.execute("UPDATE botlog SET ts=? WHERE kind='core_day' AND ts=?", (day + DAY + 2 * 3_600_000, ts))
    ts2, logged2 = days[8]  # the wait ran out: went ahead without ETH's candle, leaving ETH alone that day
    day2 = int(logged2["day"])
    logged2["targets"]["ETH/USDT"] = None
    logged2["incomplete"] = ["ETH/USDT daily close"]
    db2._conn.execute("UPDATE botlog SET data=?, ts=? WHERE kind='core_day' AND ts=?",
                      (json.dumps(logged2), day2 + DAY + 6 * 3_600_000 + 60_000, ts2))
    rec = reconcile(db2, cfg, {}, None, closes, since, end, None, [])
    waits = {w["day"]: w for w in rec.core_waits}
    assert waits[day]["waited"] and waits[day]["waited_h"] == pytest.approx(2.0, abs=0.05)
    assert waits[day]["missing"] == ["BTC/USDT daily close"] and waits[day]["first_check_h"] < 0.1
    assert waits[day2]["incomplete"] == ["ETH/USDT daily close"] and not waits[day2]["waited"]
    assert waits[day2]["first_check_h"] > 6  # no core_retry entry: the bot's first check was already too late to wait
    eth = [d for d in rec.core_days if d["day"] == day2 and d["symbol"] == "ETH/USDT"][0]
    assert "first check came 6.0h after the close, more than 6h, with no daily close: it went ahead" in eth["why"]
    text = format_reconciliation(rec, cfg)
    assert "2 days the core's data was late" in text and "went ahead without ETH/USDT daily close" in text
    assert "waited 2.0h, first missing BTC/USDT daily close" in text and "no wait (the close was already" in text


def test_the_first_logged_selection_is_in_force_from_its_learn():
    """A restart logs the selection it loaded; that selection was in force since the learn that made it."""
    t_log = 1_759_157_700_000  # 2026-09-29 14:55: the restart that logged it
    made = 1_758_969_540.0  # 2026-09-27 10:39: the learn
    logged = {"combos": [{"strategy": "momentum", "timeframe": "4h", "params": {}}], "created_at": made, "ml": False}
    today = {"combos": [{"strategy": "momentum", "timeframe": "1h", "params": {}}]}
    tl = selection_timeline([(t_log, logged)], today)
    assert tl.first == int(made * 1000)
    assert tl.at(t_log - DAY) is logged  # between the learn and the restart: the logged one, not today's
    assert tl.at(int(made * 1000) - 1) is today  # before the learn: today's selection assumed
    assert selection_timeline([(t_log, {"combos": [], "created_at": None})], today).at(t_log - DAY) is today
    assert selection_timeline([], today).at(t_log) is today and selection_timeline([], today).first is None


def test_a_daily_loss_limit_skip_is_a_modelled_check(paper_run, tmp_path):
    """Paper's daily loss limit is replayed from the backtest's own equity path: a skip for it
    that the replay didn't make is named as that, not as a live-only check."""
    cfg, market, db, *_ = paper_run
    db2 = copy_db(db, tmp_path / "limit.db")
    base = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}
    positions = db2.positions_by_signal("paper")
    sigs = sorted(db2.signals_since(0), key=lambda s: s.candle_time)
    first = next(s for s in sigs if s.id in positions and positions[s.id].status == "closed"
                 and base[(s.symbol, s.candle_time)].backtest == "taken")
    db2._conn.execute("UPDATE signals SET status='skipped', note=? WHERE id=?", ("daily loss limit reached", first.id))
    db2._conn.execute("DELETE FROM positions WHERE signal_id=?", (first.id,))
    rec = run_reconcile(paper_run, db2)
    row = {(r.symbol, r.candle_ms): r for r in rec.rows}[(first.symbol, first.candle_time)]
    assert row.paper == "skipped: daily loss limit reached" and row.backtest == "taken"
    assert row.cause.startswith("first difference: the daily loss limit (3%) - paper ") and "vs its day's start" in row.cause
    assert "; the replay " in row.cause and "vs its own (its path from the window's start" in row.cause
    assert "live-only" not in format_reconciliation(rec, cfg)


def test_an_operational_halt_is_a_live_only_check(paper_run, tmp_path):
    """Only the drawdown halt is replayed; a halt for an unknown order outcome is live-only."""
    cfg, market, db, *_ = paper_run
    db2 = copy_db(db, tmp_path / "halt.db")
    base = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}
    positions = db2.positions_by_signal("paper")
    sigs = sorted(db2.signals_since(0), key=lambda s: s.candle_time)
    first = next(s for s in sigs if s.id in positions and positions[s.id].status == "closed"
                 and base[(s.symbol, s.candle_time)].backtest == "taken")
    db2._conn.execute("UPDATE signals SET status='skipped', note=? WHERE id=?",
                      ("halted (entry #7 for X/USDT has an unknown outcome (timeout))", first.id))
    db2._conn.execute("DELETE FROM positions WHERE signal_id=?", (first.id,))
    row = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}[(first.symbol, first.candle_time)]
    assert row.cause == "first difference: a live-only check (halted (entry #7 for X/USDT has an unknown outcome (timeout)))"


def test_the_coin_list_at_the_scan_decides_what_is_replayed(paper_run, tmp_path):
    """A coin that joined paper's list when the bot woke and scanned a candle late is judged by
    the replay: the list in force at the logged scan counts, not the one at the candle's close."""
    cfg, market, db, *_ = paper_run
    db2 = copy_db(db, tmp_path / "scan.db")
    base = run_reconcile(paper_run, db2)
    row = next(r for r in base.rows if r.paper.startswith("opened") and r.backtest == "taken")
    close = row.candle_ms + HOUR
    scan_ts = close + 24 * 60_000  # the bot woke 24 minutes after the close (as on 10-04) and scanned then
    for d in (db2,):
        d._conn.execute("UPDATE botlog SET ts=? WHERE kind='scan' AND data LIKE ?",
                        (scan_ts, f'%"candle": {row.candle_ms},%'))
    others = [s for s in market.symbols if s != row.symbol]
    db2.log_bot(close - 30 * 60_000, "paper", "universe", others)  # the coin was out of the list at the close
    db2.log_bot(scan_ts, "paper", "universe", list(market.symbols))  # and back in it when the scan ran
    new = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db2).rows}[(row.symbol, row.candle_ms)]
    assert new.backtest == "taken" and new.paper == row.paper
    db3 = copy_db(db, tmp_path / "scan3.db")  # out of the list at the scan too: not replayed
    db3._conn.execute("UPDATE botlog SET ts=? WHERE kind='scan' AND data LIKE ?", (scan_ts, f'%"candle": {row.candle_ms},%'))
    db3.log_bot(close - 30 * 60_000, "paper", "universe", others)
    db3.log_bot(scan_ts + HOUR, "paper", "universe", list(market.symbols))
    gone = {(r.symbol, r.candle_ms): r for r in run_reconcile(paper_run, db3).rows}[(row.symbol, row.candle_ms)]
    assert gone.backtest.startswith("(not replayed")


def test_the_scans_candle_window_explains_a_signal_only_one_side_had(paper_run, tmp_path, monkeypatch):
    """Live computes on the candles its scan fetched, the backtest on the full history: a signal only
    one side had is re-checked on the scan's window, which says whether that explains it."""
    from tradebot import reconcile as rc
    from tradebot.weekly import reconcile_summary

    cfg, market, db, *_ = paper_run
    db2 = copy_db(db, tmp_path / "window.db")
    gone = sorted(db2.signals_since(0), key=lambda s: s.candle_time)[-3]
    db2._conn.execute("DELETE FROM signals WHERE id=?", (gone.id,))
    asked = []

    def window(strategies):
        asked.append(sorted(s.name for s in strategies))
        return 10  # 9 closed candles: shorter than breakout's warm-up, so the scan finds nothing

    monkeypatch.setattr(rc, "scan_candles", window)
    rec = run_reconcile(paper_run, db2)
    row = next(r for r in rec.rows if (r.symbol, r.candle_ms) == (gone.symbol, gone.candle_time))
    assert row.candle_window == "explains" and asked and set(map(tuple, asked)) == {("breakout",)}
    assert row.paper == ("(no signal: scanned, live found no signal (false on the last 9 candles, as the scan had "
                         "them: live's candle window explains it))")
    assert sum(bool(r.candle_window) for r in rec.rows) == 1  # rows both sides had are not re-checked
    lines, nums = reconcile_summary(rec)
    assert nums["candle_window"] == {"checked": 1, "explains": 1}
    assert nums["signal_data_differences"] == 0  # the candles agree: live used fewer of them
    assert any("explains 1 of these 1" in x for x in lines)
    assert "Backtest only: 1 scanned, live found no signal" in "\n".join(lines)
    assert "Live's candle window: of the 1 signals" in format_reconciliation(rec, cfg)


def test_a_paper_signal_the_window_explains(paper_run, tmp_path, monkeypatch):
    """The other side: paper's signal, false on the full history, true on the scan's window."""
    from tradebot import reconcile as rc
    from tradebot.models import Signal

    cfg, market, db, data, *_ = paper_run
    db2 = copy_db(db, tmp_path / "paper-window.db")
    base = {(r.symbol, r.candle_ms) for r in run_reconcile(paper_run, db2).rows}
    quiet = next(t for t in data["1h"]["ETH/USDT"].index[-80:] if ("ETH/USDT", int(t.value // 1_000_000)) not in base)
    candle = int(quiet.value // 1_000_000)
    db2.insert_signal(Signal("ETH/USDT", "1h", "breakout", "long", 100.0, 95.0, 110.0, candle, candle + HOUR + 30_000,
                             0, 0, status="skipped", note="test"))
    seen = []

    def on_window(df, i, strategy, closed, context, tf, side, min_rr):
        seen.append((int(df.index[i].value // 1_000_000), strategy.name, closed, tf, side))
        return True

    monkeypatch.setattr(rc, "entry_on_window", on_window)
    row = next(r for r in run_reconcile(paper_run, db2).rows if (r.symbol, r.candle_ms) == ("ETH/USDT", candle))
    assert (candle, "breakout", 999, "1h", "long") in seen
    assert row.candle_window == "explains"
    assert row.backtest == ("(no signal: the entry condition is false there on the full history (true on the last "
                            "999 candles, as the scan had them: live's candle window explains it))")


def test_the_entry_condition_on_the_scans_window_can_differ_from_the_full_history():
    """A long EMA computed on the scan's window starts from its first candle: here the full history's
    EMA200 still remembers an older, higher price, so only the window sees a breakout above it."""
    from tradebot.reconcile import entry_on_window
    from tradebot.strategies import make_strategy

    rows = [(200.0, 200.2, 199.8, 200.0)] * 600 + [(100.0, 100.2, 99.8, 100.0)] * 300 + [(100.0, 104.5, 99.5, 104.0)]
    df = bars(rows)
    df.iloc[-1, df.columns.get_loc("volume")] = 3000.0
    breakout, i = make_strategy("breakout", {}), len(rows) - 1
    full = breakout.populate(df, None, "1h").iloc[-1]
    assert not full["enter_long"] and full["ema_trend"] > 104.0  # the backtest: no signal
    assert entry_on_window(df, i, breakout, 260, None, "1h", "long", 1.0)  # 260 candles, all at 100: a breakout
    assert not entry_on_window(df, i, breakout, len(df), None, "1h", "long", 1.0)  # the whole history: none
    assert not entry_on_window(df, i, breakout, 249, None, "1h", "long", 1.0)  # under the warm-up: the scan skips
    assert not entry_on_window(df, i, breakout, 260, None, "1h", "long", 3.0)  # reward:risk 2.5 under the floor
