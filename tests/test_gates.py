"""The gates that replace approvals (CLAUDE.md): same-trades, deploy, research protocols, drawdown,
and the owner's Telegram messages."""
import json
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from tradebot import protocols
from tradebot.cli import main
from tradebot.db import Database
from tradebot.gates import deploy as dg
from tradebot.gates import drawdown as dd
from tradebot.gates import same_trades as st
from tradebot.models import Position

DAY = 86_400_000


# --------------------------------------------------------------------- gate 1: same trades
def test_the_stored_dataset_is_the_one_the_golden_file_was_made_on(tmp_path):
    golden = st.load_golden()
    assert golden["dataset_sha256"] == st.dataset_sha256()
    assert golden["replay"]["days"] == st.REPLAY_DAYS and golden["accepted_by"] is None
    st.make_dataset(tmp_path)  # the generator still makes the same bytes; the stored files are what counts
    assert st.dataset_sha256(tmp_path) == golden["dataset_sha256"]


def test_a_short_replay_follows_the_golden_file_and_a_changed_rule_does_not():
    golden = st.load_golden()["orders"]
    got = st.replay(days=6)
    cutoff = st.ReplayMarket().start_ms + (st.REPLAY_FROM_DAY + 6) * DAY
    assert got["signals"] and got["signals"] == [s for s in golden["signals"] if s[4] < cutoff][:len(got["signals"])]
    assert len(got["signals"]) >= sum(s[4] < cutoff - DAY for s in golden["signals"])  # (a 1d candle closes later)
    closed = [p for p in got["positions"] if p[4] == "closed"]
    assert closed and all(p in golden["positions"] for p in closed)  # the same trades, closed the same way
    assert st.replay(days=6) == got  # deterministic
    riskier = st.replay(days=6, overrides={"risk.risk_per_trade_pct": 0.5})
    assert st.differences(got, riskier)  # a trade-affecting change shows up as different orders


def _golden_copy(tmp_path, monkeypatch):
    path = tmp_path / "golden.json"
    path.write_text(st.GOLDEN.read_text())
    monkeypatch.setattr(st, "GOLDEN", path)
    return path


def test_the_same_trades_command(tmp_path, monkeypatch, capsys):
    golden = _golden_copy(tmp_path, monkeypatch)
    orders = st.load_golden()["orders"]
    monkeypatch.setattr(st, "replay", lambda days=st.REPLAY_DAYS, **_: json.loads(json.dumps(orders)))
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\n")
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "gate", "same-trades"]
    main(base)
    assert "identical to the golden file: 51 positions" in capsys.readouterr().out

    changed = json.loads(json.dumps(orders))
    changed["positions"][0][6] *= 2  # one entry twice the size
    monkeypatch.setattr(st, "replay", lambda days=st.REPLAY_DAYS, **_: changed)
    with pytest.raises(SystemExit) as e:
        main(base)
    assert e.value.code == 1 and "ORDERS DIFFER" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="no passing verdict"):
        main(base + ["--accept", "risk-double-v1"])
    assert json.loads(golden.read_text())["orders"] == orders  # untouched

    _ledger_pass(tmp_path / "state", "risk-double-v1")
    main(base + ["--accept", "risk-double-v1"])
    new = json.loads(golden.read_text())
    assert new["accepted_by"] == "risk-double-v1" and new["orders"] == changed
    main(base)
    assert "identical" in capsys.readouterr().out.splitlines()[-1]


def _ledger_pass(state_dir, test_id, family="test"):
    protocols.register(state_dir, _proto(test_id, family, run="tradebot --help"))
    protocols._append(state_dir, {"id": test_id, "kind": "run", "at": "2026-10-10 00:00:00", "command": "x"})
    protocols.record(state_dir, test_id, "pass", {"metric": 1.0})


# --------------------------------------------------------------------- gate 1: deploy
def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "t"), ("GIT_AUTHOR_EMAIL", "t@t"), ("GIT_COMMITTER_NAME", "t"),
                 ("GIT_COMMITTER_EMAIL", "t@t")):
        monkeypatch.setenv(k, v)
    agent = tmp_path / "agent"
    (agent / "tradebot" / "gates").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(agent)], check=True)
    _write_golden(agent, [[1]], None)
    _git(agent, "add", "-A")
    _git(agent, "commit", "-qm", "first")
    bot = tmp_path / "bot"
    subprocess.run(["git", "clone", "-q", str(agent), str(bot)], check=True)
    (agent / "README").write_text("a change that doesn't touch trades\n")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-qm", "docs")
    fake = tmp_path / "docker.py"  # `docker compose up` / `ps`: ps prints a container id
    fake.write_text("import sys\nprint('c0ffee' if 'ps' in sys.argv else 'built')\n")
    state = tmp_path / "state"
    state.mkdir()
    return agent, bot, state, (sys.executable, str(fake))


def _write_golden(repo, positions, accepted_by):
    (repo / dg.GOLDEN_REL).write_text(json.dumps({"accepted_by": accepted_by,
                                                  "orders": {"positions": positions, "signals": [], "core_trades": []}}))


def _deploy(repos, **kw):
    agent, bot, state, docker = repos
    return dg.deploy(agent, bot, state, run_checks=False, docker=docker, log=lambda *_: None, **kw)


def test_deploy_fast_forwards_the_bot_and_records_it(repos):
    agent, bot, state, _ = repos
    rec = _deploy(repos, dry_run=True)
    assert rec["deployed"] is False and _git(bot, "rev-parse", "HEAD") != _git(agent, "rev-parse", "HEAD")
    rec = _deploy(repos)
    assert rec["deployed"] and _git(bot, "rev-parse", "HEAD") == _git(agent, "rev-parse", "HEAD")
    assert any("not trade-affecting" in s for s in rec["steps"])
    assert json.loads((state / "deploys.jsonl").read_text().splitlines()[-1])["commit"] == rec["commit"]


def test_deploy_refuses_uncommitted_work_and_a_dirty_bot_checkout(repos):
    agent, bot, _, _ = repos
    (agent / "README").write_text("edited, not committed\n")
    with pytest.raises(dg.GateFailed, match="clean checkout"):
        _deploy(repos)
    _git(agent, "checkout", "--", "README")
    (bot / dg.GOLDEN_REL).write_text("{}")  # someone edited the bot's checkout by hand
    with pytest.raises(dg.GateFailed) as e:
        _deploy(repos)
    assert e.value.step == "bot checkout"


def test_deploy_of_different_orders_needs_a_passed_registered_test(repos):
    agent, bot, state, _ = repos
    before = _git(bot, "rev-parse", "HEAD")
    _write_golden(agent, [[2]], None)
    _git(agent, "commit", "-qam", "trades differently")
    with pytest.raises(dg.GateFailed, match="no passed registered test"):
        _deploy(repos)
    _write_golden(agent, [[2]], "change-v1")
    _git(agent, "commit", "-qam", "accepted by a test that hasn't passed")
    with pytest.raises(dg.GateFailed, match="change-v1 has no passing verdict"):
        _deploy(repos)
    assert _git(bot, "rev-parse", "HEAD") == before  # the bot was never touched
    _ledger_pass(state, "change-v1")
    rec = _deploy(repos)
    assert rec["deployed"] and any("accepted by change-v1" in s for s in rec["steps"])


# --------------------------------------------------------------------- gate 2: protocols
def _proto(test_id, family="sizing", run="tradebot --help"):
    return {"id": test_id, "family": family, "hypothesis": "h", "change": "c", "data": "d", "untouched": "u",
            "metric": "m", "pass_rule": "p", "readings": {"pass": "a", "fail": "b", "inconclusive": "c"}, "run": run}


def test_a_protocol_is_registered_run_and_recorded_once_each(tmp_path):
    state = tmp_path / "state"
    bad = _proto("x-v1")
    del bad["untouched"]
    bad["readings"] = {"pass": "a"}
    bad["run"] = "rm -rf /"
    problems = protocols.validate(bad)
    assert "missing 'untouched'" in problems and any("readings" in p for p in problems) and any("run" in p for p in problems)
    with pytest.raises(ValueError, match="not registered"):
        protocols.register(state, bad)

    protocols._append(state, {"id": "sizing-open-risk-budget-v1", "pass": False})  # an older test counts too
    entry = protocols.register(state, _proto("x-v1"))
    assert entry["trial"] == 2 and entry["family_trial"] == 1 and (state / "research" / "x-v1.protocol.json").exists()
    with pytest.raises(ValueError, match="already in the ledger"):
        protocols.register(state, _proto("x-v1"))
    with pytest.raises(ValueError, match="has not run"):
        protocols.record(state, "x-v1", "pass", {})
    code, out = protocols.run(state, "x-v1")
    assert code == 0 and "usage" in out and (state / "research" / "x-v1.out").exists()
    with pytest.raises(ValueError, match="one run per protocol"):
        protocols.run(state, "x-v1")
    with pytest.raises(ValueError, match="verdict must be"):
        protocols.record(state, "x-v1", "maybe", {})
    res = protocols.record(state, "x-v1", "fail", {"sharpe_diff": -0.02})
    assert res["trials_total"] == 2 and res["trials_in_family"] == 1 and not protocols.passed(state, "x-v1")
    with pytest.raises(ValueError, match="never edited"):
        protocols.record(state, "x-v1", "pass", {})
    text = protocols.format_status(state)
    assert "2 trials so far" in text and "sizing-open-risk-budget-v1" in text and "verdict fail" in text


def test_the_research_commands(tmp_path, capsys):
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\n")
    proto = tmp_path / "p.yaml"
    proto.write_text(json.dumps(_proto("cli-v1")))
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "research"]
    main(base + ["register", "--file", str(proto)])
    assert "Registered cli-v1 (trial 1 overall" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="already in the ledger"):
        main(base + ["register", "--file", str(proto)])
    main(base + ["run", "--id", "cli-v1"])
    main(base + ["record", "--id", "cli-v1", "--verdict", "inconclusive", "--numbers", '{"trades": 12}'])
    assert "Recorded cli-v1: INCONCLUSIVE (1 trials so far" in capsys.readouterr().out
    main(base + ["protocols"])
    assert "verdict inconclusive" in capsys.readouterr().out


# --------------------------------------------------------------------- gate 3: drawdown
def _dist():
    rng = np.random.default_rng(1)
    return {"window_start": "2023-01-01", "combined_dip_pct": 20.0, "satellite_dip_pct": 30.0,
            "longest_losing_run": 8, "trades": 300,
            "combined_returns": list(rng.normal(0.0005, 0.02, 600)), "satellite_returns": list(rng.normal(0, 0.03, 600)),
            "trade_r": list(rng.choice([-1.0, 1.5], 300))}


def test_drawdown_helpers():
    assert dd.max_drawdown([100, 120, 90, 130]) == pytest.approx(25.0)
    assert dd.longest_run([1, 1, 0, 1, 1, 1, 0]) == 3 and dd.trailing_run([1, 0, 1, 1]) == 2


def test_limits_are_set_once_and_in_the_ledger(tmp_path):
    state = tmp_path / "state"
    data = dd.set_limits(state, _dist(), reference="universe-x.json", code="abc", settings="h", paper_from_ms=0)
    assert data["limits"] == {"account_dd_pct": 30.0, "satellite_dd_pct": 45.0, "losing_run": 8}
    with pytest.raises(ValueError, match="set once, in advance"):
        dd.set_limits(state, _dist(), reference="universe-x.json", code="abc", settings="h", paper_from_ms=0)
    led = protocols.ledger(state)
    assert led[-1]["kind"] == "limits" and led[-1]["id"] == dd.LIMITS_ID
    assert dd.LIMITS_ID not in protocols.trial_ids(led)  # limits are not a trial


def _paper_db(path, totals, transfers=(), losses=0):
    db = Database(path)
    t0 = 1_790_000_000_000
    for i, (total, sat) in enumerate(totals):
        db.record_snapshot(t0 + i * DAY, "paper", total, total - sat, sat, 100.0)
    for i, amount in transfers:
        db.log_bot(t0 + i * DAY - 1000, "paper", "core_transfer", {"amount": amount})
    for k in range(losses):
        p = Position("BTC/USDT", "1h", "momentum", "long", "paper", 1.0, 100.0, 95.0, 110.0, 95.0, t0 + k * 3_600_000,
                     t0 + DAY, status="closed", closed_at=t0 + k * 3_600_000 + 60_000, r_multiple=-1.0)
        db.insert_position(p)
    return db, t0


def test_paper_state_nets_transfers_out_of_the_satellite(tmp_path):
    # the satellite drops from 350 to 300 only because 50 moved to the core: no drawdown
    db, t0 = _paper_db(tmp_path / "a.db", [(1000, 350), (1000, 300), (1010, 303)], transfers=[(1, 50.0)], losses=3)
    s = dd.paper_state(db, "paper", t0)
    assert s["satellite_dd_pct"] == pytest.approx(0.0) and s["account_dd_pct"] == pytest.approx(0.0)
    assert s["losing_run"] == 3 and s["trades"] == 3 and s["days"] == pytest.approx(2.0)
    db2, _ = _paper_db(tmp_path / "b.db", [(1000, 350), (800, 280)])
    s2 = dd.paper_state(db2, "paper", t0)
    assert s2["account_dd_pct"] == pytest.approx(20.0) and s2["satellite_dd_pct"] == pytest.approx(20.0)


def test_range_check_and_outcome(tmp_path):
    limits = dd.set_limits(tmp_path, _dist(), reference="r", code="c", settings="s", paper_from_ms=0)
    mild = {"account_dd_pct": 5.0, "satellite_dd_pct": 5.0, "losing_run": 3, "trades": 20, "days": 60}
    wild = {"account_dd_pct": 80.0, "satellite_dd_pct": 90.0, "losing_run": 40, "trades": 40, "days": 30}
    assert dd.hits(limits, mild) == [] and dd.hits(limits, wild) == ["account", "satellite", "losing_run"]
    r_mild = dd.range_check(limits, mild, ["account", "satellite", "losing_run"])
    r_wild = dd.range_check(limits, wild, ["account", "satellite", "losing_run"])
    assert all(x["in_range"] for x in r_mild.values()) and not any(x["in_range"] for x in r_wild.values())
    assert dd.range_check(limits, wild, ["account"]) == dd.range_check(limits, wild, ["account"])  # seeded
    assert dd.outcome(r_mild, True) == "in range" and dd.outcome(r_wild, True) == "outside range"
    assert dd.outcome(r_mild, False) == "mechanics differ"


def test_the_drawdown_gate_command(tmp_path, monkeypatch, capsys):
    import tradebot.cli as cli

    state = tmp_path / "state"
    state.mkdir()
    db, t0 = _paper_db(state / "tradebot.db", [(1000, 350), (1100, 380), (700, 250)])
    db.close()
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\n")
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "gate", "drawdown"]
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    with pytest.raises(SystemExit, match="No drawdown limits yet"):
        main(base)
    dd.set_limits(state, _dist(), reference="r", code="c", settings="s", paper_from_ms=t0)
    monkeypatch.chdir(tmp_path)  # the report goes to reports/

    def no_reconcile(*a, **k):
        raise ValueError("no paper history")

    monkeypatch.setattr(cli, "_reconcile_run", no_reconcile)
    with pytest.raises(SystemExit) as e:
        main(base)
    out = capsys.readouterr().out
    assert e.value.code == 12 and "Drawdown gate hit (account)" in out and "mechanics differ" in out
    assert list((tmp_path / "reports").glob("drawdown-*.txt"))
    with pytest.raises(SystemExit) as e:  # the same dip is handled once
        main(base)
    assert e.value.code == 12 and "Already handled" in capsys.readouterr().out
    assert len(dd.events(state)) == 1


def test_set_limits_from_the_reference_backtest(tmp_path, monkeypatch, capsys):
    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.learning import SELECTION_FILE
    from tradebot.universe import selection_payload

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
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "gate", "drawdown", "--set-limits",
            "--synthetic", "--reference", str(ref), "--paper-from", "2026-10-01"]
    main(base)
    out = capsys.readouterr().out
    assert "Drawdown limits set (worst dips x 1.5" in out and "watching paper from 2026-10-01 00:00 UTC" in out
    lim = dd.load_limits(state)
    assert lim["paper_from_ms"] == int(pd.Timestamp("2026-10-01", tz="UTC").value // 1_000_000)
    assert lim["limits"]["account_dd_pct"] == pytest.approx(lim["backtest"]["combined_dip_pct"] * 1.5, abs=0.01)
    with pytest.raises(SystemExit, match="already set"):
        main(base)


# --------------------------------------------------------------------- gate 4: telling the owner
def test_notify_needs_telegram_and_sends_plain_text(tmp_path, monkeypatch, capsys):
    from tradebot.notify.telegram import TelegramNotifier

    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\nname: agent\n")
    env = tmp_path / "none.env"
    with pytest.raises(SystemExit, match="Telegram is not configured"):
        main(["--config", str(conf), "--env", str(env), "notify", "--text", "hello"])
    env.write_text("TELEGRAM_BOT_TOKEN=t\nTELEGRAM_CHAT_ID=42\n")
    sent = []
    monkeypatch.setattr(TelegramNotifier, "_call", lambda self, method, **kw: sent.append((method, kw)))
    main(["--config", str(conf), "--env", str(env), "notify", "--text", "gate 1 failed: <lint>"])
    assert capsys.readouterr().out.strip() == "sent"
    assert sent == [("sendMessage", {"chat_id": "42", "text": "[agent] gate 1 failed: <lint>",
                                     "disable_web_page_preview": True})]


def test_the_weekly_telegram_summary():
    from tradebot.weekly import telegram_summary

    text = telegram_summary("2026-10-12", "abc1234", ["- **Uptime** 99%", "- Full reports: reports/weekly/x"], [])
    assert text.splitlines()[0] == "Weekly check-in 2026-10-12 (code abc1234)"
    assert "**" not in text and "Full reports" not in text and text.endswith("Needs you: nothing")
    long = telegram_summary("d", "c", ["- x" * 2000], ["a", "b"])
    assert len(long) < 3600 and long.endswith("(STATUS.md has the rest)")
