import json

from tradebot.backtest.selection import ComboResult, Selection
from tradebot.cli import main
from tradebot.learning import SELECTION_FILE
from tradebot.universe import selection_payload
from tradebot.weekly import backlog_next, status_entry


def test_weekly_writes_a_status_entry_and_remembers_the_reference(tmp_path, monkeypatch, capsys):
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
    status = tmp_path / "STATUS.md"
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "weekly", "--synthetic",
            "--status-file", str(status), "--out-dir", str(tmp_path / "weekly")]
    main(base + ["--reference", str(ref)])
    out = capsys.readouterr().out
    text = status.read_text()
    assert text.startswith("# Status") and text.count("- weekly check-in (`tradebot weekly`)") == 1
    for part in ("**What changed**", "**What the numbers say**", "Reproduce:", "**What needs you**",
                 "**What's next** (BACKLOG.md)", "tradebot reconcile --since", "--universe-file " + str(ref),
                 "Uptime 0% of the week", "Frozen reference rerun", "Strategy selection: saved with the run"):
        assert part in text, part
    assert "paper can't be compared" in text  # the bot never ran here: flagged for the owner
    assert len(text.splitlines()) < 60  # under a page
    assert list((tmp_path / "weekly").glob("*/reconcile.txt")) and list((tmp_path / "weekly").glob("*/portfolio-backtest.txt"))
    assert "Appended to" in out

    # next week: the reference is remembered; a setting change is reported
    conf.write_text(conf.read_text() + "risk:\n  risk_per_trade_pct: 0.5\n")
    main(base)
    second = status.read_text().split("- weekly check-in (`tradebot weekly`)")[-1]
    assert "setting risk.risk_per_trade_pct: saved 1.0, now 0.5" in second
    assert "Settings changed since last week" in second
    assert "The frozen reference rerun moved" not in second or "Sharpe" in second


def test_backlog_and_entry_helpers(tmp_path):
    b = tmp_path / "BACKLOG.md"
    b.write_text("# Backlog\n- [x] **done thing** - finished\n- [ ] **First open** - why\n  - criterion\n"
                 "- [ ] **Second open**\n- [ ] Third\n- [ ] Fourth\n")
    assert backlog_next(b) == ["First open", "Second open", "Third"]
    e = status_entry(date="2026-10-04", code="abc", settings="123", changed=[], numbers=["- n"], reproduce=["cmd"],
                     needs=[], next_items=[])
    assert "nothing since last week's check-in" in e and "- nothing" in e and "    cmd" in e
