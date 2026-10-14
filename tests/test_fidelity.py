import json
import shutil

import pandas as pd
import pytest

from tradebot import fidelity
from tradebot.backtest.selection import ComboResult, Selection
from tradebot.cli import main
from tradebot.data import OHLCVStore
from tradebot.data.synthetic import generate_ohlcv
from tradebot.learning import SELECTION_FILE
from tradebot.protocols import ledger, trial_ids
from tradebot.universe import selection_payload


def _run(selected: list[str], is_r: float = 0.1, params: dict | None = None) -> dict:
    keys = ["breakout@4h", "momentum@1d", "momentum@1h"]
    return {"data": {"1d": {"BTC/USDT": [10, "2026-09-01 00:00:00+00:00", "2026-09-26 00:00:00+00:00"]}},
            "context": True,
            "combos": [{"key": k, "params": (params or {}).get(k, {}), "selected": k in selected,
                        "in_sample": [100, is_r if k == "breakout@4h" else 0.2],
                        "out_of_sample": [40, 0.3], "reasons": [] if k in selected else ["in-sample loses"]}
                       for k in keys]}


DEPLOY = {"at": "2026-10-14 23:40:00", "commit": "51c0a0b", "deployed": True}


def _rec(before: list[str], after: list[str], fix: str = "48d1c02" + "0" * 33) -> dict:
    rec = fidelity.compare(_run(before, 0.001), _run(after, 0.09))
    return {"fix": fix, "subject": "Backtest: the take-profit is a touch", "before_code": "f" * 40,
            "universe_file": "reports/universe-ref.json", "settings": "abc", **rec}


def test_compare_names_what_the_fix_adds_and_drops():
    rec = _rec(["momentum@1d", "momentum@1h"], ["breakout@4h", "momentum@1d"])
    assert rec["added"] == ["breakout@4h"] and rec["dropped"] == ["momentum@1h"]
    assert rec["before"] == ["momentum@1d", "momentum@1h"] and rec["symbols"] == ["BTC/USDT"]
    text = fidelity.format_comparison(rec)
    assert "Change: added breakout@4h; dropped momentum@1h" in text
    row = next(x for x in text.splitlines() if x.strip().startswith("breakout@4h"))
    assert "+0.001R" in row and "+0.090R" in row and row.endswith("added")
    assert not any(x.strip().startswith("momentum@1d") for x in text.splitlines())  # unchanged rows are left out
    same = _rec(["momentum@1d"], ["momentum@1d"])
    assert same["added"] == same["dropped"] == [] and fidelity.change_text(same) == "the same strategies"


def test_a_filter_switched_on_a_selected_combination_is_a_change():
    """learn's btc_filter: auto picks the filter per combination: a fix can flip it with the same keys."""
    rec = fidelity.compare(_run(["momentum@1d"]), _run(["momentum@1d"], params={"momentum@1d": {"btc_filter": True}}))
    assert rec["dropped"] == ["momentum@1d"] and rec["added"] == ['momentum@1d {"btc_filter": true}']
    rec.update(fix="f" * 40, before_code="e" * 40, universe_file="u.json", settings="abc")
    row = next(x for x in fidelity.format_comparison(rec).splitlines() if x.strip().startswith("momentum@1d"))
    assert row.endswith("parameters {} -> {'btc_filter': True}")


def test_coverage_names_coins_without_candles_and_candles_that_stop_early():
    day = 86_400_000
    end = int(pd.Timestamp("2026-09-27 15:13", tz="UTC").value // 1_000_000)
    data = {"1d": {"A/USDT": [9, "2026-09-01 00:00:00+00:00", "2026-09-26 00:00:00+00:00"],
                   "B/USDT": [8, "2026-09-01 00:00:00+00:00", "2026-09-25 00:00:00+00:00"]},
            "1h": {"A/USDT": [9, "2026-09-26 00:00:00+00:00", "2026-09-27 14:00:00+00:00"]}}
    cov = fidelity.coverage(data, ["A/USDT", "B/USDT"], end)
    assert cov["1d"] == {"coins": 2, "missing": [], "ends_early": ["B/USDT"]}  # the 09-26 candle had closed
    assert cov["1h"] == {"coins": 1, "missing": ["B/USDT"], "ends_early": []}
    assert fidelity.coverage_text(cov) == ("coins per timeframe: 1d 2 (1 stop before the data end), "
                                           "1h 1 (no candles for B/USDT)")
    assert day  # the end is mid-day: the last daily candle opened the day before


def test_a_fix_is_recorded_once_and_is_not_a_trial(tmp_path):
    rec = _rec(["momentum@1d"], ["breakout@4h", "momentum@1d"])
    entry = fidelity.record(tmp_path, rec)
    assert entry["id"] == "fidelity-48d1c02" and entry["kind"] == "selection" and entry["added"] == ["breakout@4h"]
    with pytest.raises(ValueError, match="already in the ledger"):
        fidelity.record(tmp_path, rec)
    assert fidelity.recorded(tmp_path, rec["fix"])["at"] == entry["at"]
    assert trial_ids(ledger(tmp_path)) == []  # nothing is chosen on it: not a test
    assert [e["id"] for e in fidelity.unreported(tmp_path, [])] == ["fidelity-48d1c02"]
    assert fidelity.unreported(tmp_path, ["fidelity-48d1c02"]) == []
    line = fidelity.lead_line(entry, DEPLOY)
    assert line.startswith("fidelity fix 48d1c02 (Backtest: the take-profit is a touch) changes learn's selection")
    assert "added breakout@4h" in line and "ledger fidelity-48d1c02" in line
    assert "deployed 2026-10-14 23:40 UTC in 51c0a0b" in line


def test_deployed_in_finds_the_first_deploy_that_contains_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(fidelity, "_contains", lambda commit, fix: commit >= "b")
    assert fidelity.deployed_in(tmp_path, "f" * 40) is None  # no deploys yet
    rows = [{"at": "1", "commit": "a", "deployed": True}, {"at": "2", "commit": "b", "deployed": False},
            {"at": "3", "commit": "c", "deployed": True}, {"at": "4", "commit": "d", "deployed": True}]
    (tmp_path / "deploys.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert fidelity.deployed_in(tmp_path, "f" * 40)["at"] == "3"  # a failed gate deploys nothing


def test_measure_runs_learns_selection_on_each_commits_own_copy(tmp_path, monkeypatch):
    """Both sides run in their own copy, read the same cached candles and never change the cache."""
    data = tmp_path / "data"
    store = OHLCVStore(data, "binance")
    for i, sym in enumerate(["BTC/USDT", "AAA/USDT", "BBB/USDT"]):
        store.save(sym, "1d", generate_ohlcv(700, "1d", seed=i + 1, start="2023-01-01"))
    store.save("BTC/USDT", "1h", generate_ohlcv(24 * 60, "1h", seed=9, start="2024-10-01"))
    end_ms = int(store.load("BTC/USDT", "1d").index[-1].value // 1_000_000) + 86_400_000
    before_files = {p: p.stat().st_mtime_ns for p in data.rglob("*.csv.gz")}
    conf = tmp_path / "c.yaml"
    conf.write_text("timeframes: [1d]\nml:\n  enabled: false\ndata:\n  history_days: {1d: 900}\n"
                    "selection:\n  min_trades_in_sample: 3\n  min_trades_out_of_sample: 1\n")
    uni = tmp_path / "universe.json"
    uni.write_text(json.dumps({"symbols": ["BTC/USDT", "AAA/USDT", "BBB/USDT"], "data_end_ms": end_ms}))
    copies = []

    def extract(commit, dest):  # "b" is the fix: in its copy, learn selects every combination
        copies.append(commit)
        shutil.copytree(fidelity.REPO / "tradebot", dest / "tradebot", ignore=shutil.ignore_patterns("__pycache__"))
        if commit == "b" * 40:
            with open(dest / "tradebot" / "backtest" / "selection.py", "a") as fh:
                fh.write("\n_real = run_selection\n\n\ndef run_selection(*a, **k):\n    sel = _real(*a, **k)\n"
                         "    for c in sel.combos:\n        c.selected = True\n    return sel\n")

    monkeypatch.setattr(fidelity, "_extract", extract)
    rec = fidelity.measure("a" * 40, "b" * 40, conf, uni, data, fetch=False)
    assert copies == ["a" * 40, "b" * 40]
    assert rec["symbols"] == ["AAA/USDT", "BBB/USDT", "BTC/USDT"]
    assert rec["coverage"]["1d"] == {"coins": 3, "missing": [], "ends_early": []}
    unselected = sorted(r["key"] for r in rec["rows"] if not r["before"]["selected"])
    assert unselected and rec["added"] == unselected and rec["dropped"] == []  # each side ran its own copy
    assert len(rec["rows"]) == 4 and all(r["after"]["selected"] for r in rec["rows"])
    assert all(r["before"]["in_sample"] == r["after"]["in_sample"] for r in rec["rows"])  # the same candles
    assert {p: p.stat().st_mtime_ns for p in data.rglob("*.csv.gz")} == before_files


def test_measure_reports_a_side_that_fails(tmp_path, monkeypatch):
    def extract(commit, dest):
        (dest / "tradebot").mkdir()
        (dest / "tradebot" / "__init__.py").write_text("raise SystemExit('broken copy')\n")

    monkeypatch.setattr(fidelity, "_extract", extract)
    with pytest.raises(RuntimeError, match="the code before the fix failed"):
        fidelity.measure("a" * 40, "b" * 40, tmp_path / "c.yaml", tmp_path / "u.json", tmp_path)


def test_weekly_leads_with_a_fixs_selection_change_once(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    sel = Selection(1_700_000_000.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)])
    sel.save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 3\nml:\n  enabled: false\n"
                    "data:\n  history_days: {1d: 600}\n")
    ref = tmp_path / "universe-ref.json"
    ref.write_text(json.dumps({"symbols": ["BTC/USDT", "SOL/USDT"], "data_end_ms": 1_748_736_000_000,
                               "source": "saved run", "selection": ["momentum@1d"],
                               "selection_full": selection_payload(sel)}))
    fidelity.record(state, _rec(["momentum@1d"], ["breakout@4h", "momentum@1d"]))
    fidelity.record(state, _rec(["momentum@1d"], ["momentum@1d"], fix="1234567" + "0" * 33))  # no change: no line
    sent = []
    monkeypatch.setattr("tradebot.cli._telegram_send", lambda cfg, text: sent.append(text))
    monkeypatch.setattr(fidelity, "_contains", lambda commit, fix: True)
    status = tmp_path / "STATUS.md"
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "weekly", "--synthetic", "--notify",
            "--status-file", str(status), "--out-dir", str(tmp_path / "weekly"), "--reference", str(ref)]
    main(base)  # not deployed yet: not reported, kept for later
    assert "fidelity fix" not in status.read_text() and "fidelity" not in sent[0]
    assert json.loads((state / "research" / "weekly.json").read_text())["fidelity_reported"] == []
    # deployed; meanwhile learn re-selected and the code moved on: the fix's line still comes first
    (state / "deploys.jsonl").write_text(json.dumps(DEPLOY) + "\n")
    saved = json.loads((state / "research" / "weekly.json").read_text())
    saved["code"], saved["selection"]["created"] = "0000000", 1.0
    (state / "research" / "weekly.json").write_text(json.dumps(saved))
    main(base)
    first = status.read_text().split("- weekly check-in (`tradebot weekly`)")[-1].split("**What changed**")[1]
    first = first.split("**What the numbers say**")[0].strip().splitlines()
    assert len(first) >= 3 and any(x.startswith("- learn re-selected") for x in first)
    assert any(x.startswith("- code 0000000 ->") for x in first)
    assert first[0].startswith("- fidelity fix 48d1c02 (") and "added breakout@4h" in first[0]
    assert not any("1234567" in x for x in first)
    assert sent[1].splitlines()[1].startswith("Changed: fidelity fix 48d1c02")
    needs = status.read_text().split("**What needs you**")[-1].split("**What's next**")[0]
    assert "fidelity" not in needs  # not a stop-and-ask
    saved = json.loads((state / "research" / "weekly.json").read_text())
    assert saved["fidelity_reported"] == ["fidelity-1234567", "fidelity-48d1c02"]
    main(base)  # next week: reported once
    second = status.read_text().split("- weekly check-in (`tradebot weekly`)")[-1]
    assert "fidelity fix" not in second
