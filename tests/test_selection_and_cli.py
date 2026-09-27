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
