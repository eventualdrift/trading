import json

import pytest

from tradebot.research import SIZING_TEST, previous_result, record_study, sizing_verdict


def stats(comb_sharpe, core_sharpe, comb_dd, matched_dd):
    return {"combined": {"sharpe": comb_sharpe, "max_dd_pct": comb_dd},
            "core": {"sharpe": core_sharpe, "max_dd_pct": 31.0},
            "matched": {"sharpe": core_sharpe, "max_dd_pct": matched_dd, "k": 0.7}}


def test_sizing_verdict_applies_the_preregistered_margins():
    assert sizing_verdict(stats(0.97, 0.86, 25, 23))["pass"]  # Sharpe +0.11
    assert not sizing_verdict(stats(0.95, 0.86, 25, 23))["pass"]  # +0.09: not enough
    assert sizing_verdict(stats(0.80, 0.86, 20, 23))["pass"]  # dip 3 points smaller
    v = sizing_verdict(stats(0.87, 0.86, 22, 23))  # better, but inside the margins
    assert not v["pass"] and v["pass_without_margins"]
    assert SIZING_TEST["variants_tested"] == 1 and SIZING_TEST["budget_pct"] == 3.0


def test_first_result_is_kept_and_reruns_are_logged(tmp_path):
    study = {"test": SIZING_TEST, "verdict": {"pass": False}, "runs": {}}
    record_study(tmp_path, study)
    record_study(tmp_path, {**study, "verdict": {"pass": True}}, rerun=True)
    assert previous_result(tmp_path)["verdict"]["pass"] is False  # the one-time result stands
    ledger = [json.loads(line) for line in (tmp_path / "research" / "ledger.jsonl").read_text().splitlines()]
    assert [e["rerun"] for e in ledger] == [False, True]


@pytest.mark.parametrize("already_ran", [False, True])
def test_cli_research_sizing_runs_once(tmp_path, monkeypatch, capsys, already_ran):
    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.cli import main
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 3\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 700}\n")
    if already_ran:
        record_study(state, {"test": SIZING_TEST, "verdict": {"pass": False}, "runs": {}})
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "research", "sizing", "--synthetic",
          "--days", "700"])
    out = capsys.readouterr().out
    if already_ran:
        assert "already ran" in out and "first result stands" in out
    else:
        assert "VERDICT:" in out and "Pre-registered sizing test" in out and "not recorded" in out
        assert previous_result(state) is None  # synthetic data never uses up the one-time test


def test_cli_research_core_reports_without_changing_anything(tmp_path, monkeypatch, capsys):
    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.cli import main
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 3\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 700}\n")
    before = conf.read_text()
    main(["--config", str(conf), "--env", str(tmp_path / "none.env"), "research", "core", "--synthetic",
          "--days", "700"])
    out = capsys.readouterr().out
    assert "Core robustness - REPORTING ONLY" in out and "Trend lengths scaled" in out
    assert conf.read_text() == before and not (state / "research").exists()  # synthetic: nothing recorded
