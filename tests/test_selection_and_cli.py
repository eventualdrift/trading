import pytest

from tradebot.backtest.selection import Selection, run_selection
from tradebot.cli import main
from tradebot.data.synthetic import generate_ohlcv


def test_selection_runs_and_roundtrips(cfg, tmp_path):
    cfg.selection.min_trades_in_sample = 5
    cfg.selection.min_trades_out_of_sample = 2
    datasets = {"1h": {f"S{i}/USDT": generate_ohlcv(4000, "1h", seed=i) for i in range(3)}}
    sel = run_selection(datasets, cfg, log=lambda *_: None)
    assert len(sel.combos) == len(cfg.strategies) == 4
    for c in sel.combos:
        assert c.selected == (not c.reasons)
        assert c.in_sample["trades"] + c.out_of_sample["trades"] > 0
    path = tmp_path / "sel.json"
    sel.save(path)
    again = Selection.load(path)
    assert [c.key for c in again.combos] == [c.key for c in sel.combos]
    assert again.timeframes() == sel.timeframes()


def test_trades_straddling_the_split_are_purged(cfg):
    """Review #11: an in-sample trade must not use out-of-sample prices."""
    from tradebot.backtest.selection import run_combo
    from tradebot.strategies import make_strategy

    df = generate_ohlcv(4000, "1h", seed=2)
    is_t, oos_t, _ = run_combo({"A/USDT": df}, "breakout", {}, "1h", cfg)
    warmup = make_strategy("breakout").warmup
    split = warmup + int((len(df) - warmup) * cfg.selection.in_sample_fraction)
    assert is_t and oos_t
    assert all(t.exit_time < df.index[split] for t in is_t)
    assert all(t.signal_idx >= split for t in oos_t)


def test_symbol_score_uses_only_admitted_trades(cfg):
    from tradebot.backtest.selection import run_combo

    df = generate_ohlcv(4000, "1h", seed=2)
    is_t, oos_t, per_symbol = run_combo({"A/USDT": df}, "breakout", {}, "1h", cfg)
    admitted = is_t + oos_t
    assert per_symbol["A/USDT"] == pytest.approx(sum(t.r_multiple for t in admitted) / len(admitted))


def test_demo_runs_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["demo", "--days", "240", "--sim-days", "4", "--symbols-n", "2"])
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "Go-live readiness" in out


def test_portfolio_backtest_reruns_on_a_frozen_universe(tmp_path, monkeypatch, capsys):
    import json

    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 4\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 600}\n")
    ufile = tmp_path / "universe.json"
    end_ms = 1_748_736_000_000  # 2025-06-01, inside the synthetic history (2024-01 to 2025-09)
    ufile.write_text(json.dumps({"symbols": ["BTC/USDT", "SOL/USDT"], "data_end_ms": end_ms,
                                 "source": "saved run", "selection": ["momentum@1d"]}))
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "portfolio-backtest", "--synthetic",
          "--days", "600", "--since", "", "--universe-file", str(ufile)])
    out = capsys.readouterr().out
    assert "Coins: BTC, SOL" in out and "Data to 2025-06-01 00:00 UTC (frozen" in out
    assert "to 2025-05-31)" in out  # every series stops at the frozen data end date


def test_frozen_rerun_reports_and_stamps_code_and_settings(tmp_path, monkeypatch, capsys):
    import json

    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 4\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 600}\n")
    old = tmp_path / "universe-old.json"  # saved before code/settings were recorded
    old.write_text(json.dumps({"symbols": ["BTC/USDT", "SOL/USDT"], "data_end_ms": 1_748_736_000_000,
                               "source": "saved run", "selection": ["momentum@1d"]}))
    args = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "portfolio-backtest", "--synthetic",
            "--days", "600", "--since", ""]
    main(args + ["--universe-file", str(old)])
    out = capsys.readouterr().out
    assert "predates code/settings recording" in out and "Code: " in out
    stamped = [p for p in tmp_path.glob("universe-old@*.json")]
    assert len(stamped) == 1
    saved = json.loads(stamped[0].read_text())
    assert saved["code"] and saved["config"]["costs"]["entry_order"] == "market"
    assert saved["symbols"] == ["BTC/USDT", "SOL/USDT"] and saved["data_end_ms"] == 1_748_736_000_000
    main(args + ["--universe-file", str(stamped[0])])  # same code and settings: nothing to flag
    out = capsys.readouterr().out
    assert "predates" not in out and "Different code" not in out and "Setting changed" not in out
    conf.write_text(conf.read_text() + "risk:\n  risk_per_trade_pct: 0.5\n")  # a setting changes
    main(args + ["--universe-file", str(stamped[0])])
    assert "Setting changed since the saved run - risk.risk_per_trade_pct: saved 1.0, now 0.5" in capsys.readouterr().out
    names = sorted(p.name for p in tmp_path.glob("universe-old@*.json"))
    assert len(names) == 2 and all(n.count("@") == 1 for n in names)  # re-stamped from the original name


def test_research_note_leaves_the_result_unchanged(tmp_path, monkeypatch, capsys):
    import json

    from tradebot.research import SIZING_TEST, previous_result, record_study

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\n")
    record_study(tmp_path / "state", {"test": SIZING_TEST, "verdict": {"pass": False}, "runs": {}})
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "research", "note",
          "--id", SIZING_TEST["id"], "--text", "evaluated on the unsaved 12:40 coin list"])
    prev = previous_result(tmp_path / "state")
    assert prev["verdict"]["pass"] is False and prev["notes"][0]["note"] == "evaluated on the unsaved 12:40 coin list"
    ledger = [json.loads(x) for x in (tmp_path / "state" / "research" / "ledger.jsonl").read_text().splitlines()]
    assert [e.get("kind") for e in ledger] == [None, "note"]


def test_settings_hash_does_not_depend_on_timeframe_order():
    from tradebot.config import BotConfig
    from tradebot.provenance import config_hash, config_snapshot

    a, b = BotConfig(), BotConfig()
    a.timeframes, b.timeframes = ["4h", "1d"], ["1d", "4h", "1d"]
    assert config_snapshot(b)["timeframes"] == ["4h", "1d"]
    assert config_hash(config_snapshot(a)) == config_hash(config_snapshot(b))
    b.timeframes = ["1d"]  # a different set is a different run
    assert config_hash(config_snapshot(a)) != config_hash(config_snapshot(b))


def test_frozen_rerun_uses_the_saved_selection(tmp_path, monkeypatch, capsys):
    import json

    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.learning import SELECTION_FILE
    from tradebot.universe import selection_payload

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("breakout", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)  # today's
    saved = Selection(1_700_000_000.0, [ComboResult("momentum", "1d", {"btc_filter": True}, {}, {}, 1, 1.0, True),
                                        ComboResult("trend", "1d", {}, {}, {}, 1, 1.0, False)])
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 4\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 600}\n")
    ufile = tmp_path / "universe-x.json"
    ufile.write_text(json.dumps({"symbols": ["BTC/USDT", "SOL/USDT"], "data_end_ms": 1_748_736_000_000,
                                 "source": "saved run", "selection": ["momentum@1d"],
                                 "selection_full": selection_payload(saved)}))
    args = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "portfolio-backtest", "--synthetic",
            "--days", "600", "--since", "", "--universe-file", str(ufile)]
    main(args)
    out = capsys.readouterr().out
    assert "Strategies: momentum@1d" in out and "Strategy selection used: saved with the run" in out
    assert "today's selection is breakout@1d; --current-selection reruns with it" in out
    assert "differs from the saved run's" not in out
    stamped = json.loads(next(tmp_path.glob("universe-x@*.json")).read_text())
    assert stamped["selection_full"]["combos"][0]["params"] == {"btc_filter": True}  # filters and parameters kept
    main(args + ["--current-selection"])
    out = capsys.readouterr().out
    assert "Strategies: breakout@1d" in out and "differs from the saved run's" in out


def test_an_older_universe_files_selection_is_recovered_from_the_activity_log(cfg):
    from tradebot.db import Database
    from tradebot.universe import saved_selection

    frozen = {"selection": ["momentum@4h", "trend@1d"], "data_end_ms": 1_759_000_000_000}
    assert saved_selection(frozen, cfg) is None  # no activity log: nothing to recover
    db = Database(cfg.state_path / "tradebot.db")
    combos = [{"strategy": "trend", "timeframe": "1d", "params": {"fast": 20}},
              {"strategy": "momentum", "timeframe": "4h", "params": {}}]
    db.log_bot(1, "paper", "selection", {"combos": combos[:1], "created_at": 1_758_000_000.0, "ml": False})
    db.log_bot(2, "paper", "selection", {"combos": combos, "created_at": 1_758_500_000.0, "ml": False})
    db.log_bot(3, "paper", "selection", {"combos": combos, "created_at": 1_759_500_000.0, "ml": False})  # after the run
    sel, source = saved_selection(frozen, cfg)
    assert sel.created_at == 1_758_500_000.0 and source.startswith("recovered from the bot's activity log")
    assert {(c.key, tuple(c.params.items())) for c in sel.selected} == {("trend@1d", (("fast", 20),)),
                                                                         ("momentum@4h", ())}


def test_code_version_inside_a_docker_image(monkeypatch):
    from tradebot.provenance import code_version

    monkeypatch.setenv("TRADEBOT_COMMIT", "abc1234")
    assert code_version() == "abc1234"
    monkeypatch.setenv("TRADEBOT_COMMIT", "unknown")  # built without the commit: fall back to git
    assert code_version() != "unknown"


def test_an_observer_config_reads_the_bot_and_never_runs_it(tmp_path, monkeypatch, capsys):
    import pytest

    from tradebot.cli import _bot_db
    from tradebot.config import load_config
    from tradebot.db import Database

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    (tmp_path / "bot").mkdir()
    Database(tmp_path / "bot" / "tradebot.db")  # the running bot's
    conf = tmp_path / "agent.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'agent'}\nobserve_state_dir: {tmp_path / 'bot'}\n"
                    f"learning:\n  follow_state_dir: {tmp_path / 'bot'}\n")
    cfg = load_config(conf, env_file=None)
    db = _bot_db(cfg)
    assert db.readonly and db.path == str(tmp_path / "bot" / "tradebot.db")
    with pytest.raises(SystemExit, match="never runs a bot"):
        main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "run"])
    with pytest.raises(SystemExit, match="uses the strategies in"):
        main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "learn"])  # learn refuses too


def test_report_says_which_ml_model_is_in_force(tmp_path):
    import json
    from types import SimpleNamespace

    from tradebot.cli import ml_status

    rep = tmp_path / "model_report.json"
    rep.write_text(json.dumps({"trained_at": 1_791_049_200, "promoted": False, "reason": "worse than current model"}))
    model = SimpleNamespace(threshold=0.55, report=SimpleNamespace(trained_at=1_790_444_400,
                                                                   trained_until="2026-09-20T00:00:00",
                                                                   confidence_scaling=True))
    text = ml_status(model, rep)
    assert "ML filter: ACTIVE - model trained 2026-09-26" in text and "threshold 55%" in text
    assert "Last learn's ML training (2026-10-03 17:40 UTC): not deployed - worse than current model" in text
    assert "not active" in ml_status(None, tmp_path / "none.json")
