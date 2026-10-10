"""Gate 2, the research gate: registered tests the agent runs on its own.

A protocol is written from the fixed template in CLAUDE.md (hypothesis, the change, data and why
it is untouched, metric, pass rule, how each outcome is read, the one command that runs it). It is
registered in the ledger before it runs; it runs once (a second run is refused); its verdict is
recorded once. Every registration counts as a trial, and results are reported with the trial
counts. A change that passed applies through gate 1, whose deploy step checks ``passed()``.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

REQUIRED = ("id", "family", "hypothesis", "change", "data", "untouched", "metric", "pass_rule", "readings", "run")
VERDICTS = ("pass", "fail", "inconclusive")


def folder(state_dir) -> Path:
    f = Path(state_dir) / "research"
    f.mkdir(parents=True, exist_ok=True)
    return f


def ledger(state_dir) -> list[dict]:
    p = folder(state_dir) / "ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _append(state_dir, entry: dict) -> dict:
    with open(folder(state_dir) / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def trial_ids(entries: list[dict], family: str | None = None) -> list[str]:
    """Every test ever registered or run, in order: the older pre-registered tests count too."""
    ids = []
    for e in entries:
        if e.get("kind") in (None, "registration", "result") and e.get("id") and e["id"] not in ids:
            fam = (e.get("protocol") or {}).get("family")
            if family is None or fam == family:
                ids.append(e["id"])
    return ids


def validate(proto: dict) -> list[str]:
    problems = [f"missing '{k}'" for k in REQUIRED if not proto.get(k)]
    readings = proto.get("readings") or {}
    if not isinstance(readings, dict) or not readings.get("pass") or not readings.get("fail"):
        problems.append("readings needs at least 'pass' and 'fail'")
    if proto.get("run") and not str(proto["run"]).startswith("tradebot "):
        problems.append("run must be a single `tradebot ...` command")
    return problems


def register(state_dir, proto: dict) -> dict:
    problems = validate(proto)
    if problems:
        raise ValueError("protocol not registered: " + "; ".join(problems))
    entries = ledger(state_dir)
    if any(e.get("id") == proto["id"] for e in entries):
        raise ValueError(f"{proto['id']} is already in the ledger: a second look needs a new id (and counts as a trial)")
    canon = json.dumps(proto, sort_keys=True)
    entry = {"id": proto["id"], "kind": "registration", "at": _now(), "protocol": proto,
             "sha256": hashlib.sha256(canon.encode()).hexdigest(),
             "trial": len(trial_ids(entries)) + 1,
             "family_trial": len(trial_ids(entries, proto["family"])) + 1}
    (folder(state_dir) / f"{proto['id']}.protocol.json").write_text(json.dumps(proto, indent=2))
    return _append(state_dir, entry)


def _entries(state_dir, test_id: str, kind: str) -> list[dict]:
    return [e for e in ledger(state_dir) if e.get("id") == test_id and e.get("kind") == kind]


def run(state_dir, test_id: str, cwd: str | None = None) -> tuple[int, str]:
    """Run a registered protocol's command, once. The run is in the ledger before it starts, so a
    second run is refused even if the first one crashed."""
    reg = _entries(state_dir, test_id, "registration")
    if not reg:
        raise ValueError(f"{test_id} is not registered")
    if _entries(state_dir, test_id, "run"):
        raise ValueError(f"{test_id} has already run: one run per protocol")
    cmd = shlex.split(reg[-1]["protocol"]["run"])
    _append(state_dir, {"id": test_id, "kind": "run", "at": _now(), "command": " ".join(cmd)})
    proc = subprocess.run([sys.executable, "-m", "tradebot.cli", *cmd[1:]], cwd=cwd, capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    (folder(state_dir) / f"{test_id}.out").write_text(out)
    return proc.returncode, out


def record(state_dir, test_id: str, verdict: str, numbers: dict, note: str = "") -> dict:
    """The verdict, once, from the registered pass rule applied to the run's output."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    reg = _entries(state_dir, test_id, "registration")
    if not reg:
        raise ValueError(f"{test_id} is not registered")
    if not _entries(state_dir, test_id, "run"):
        raise ValueError(f"{test_id} has not run (tradebot research run --id {test_id})")
    if _entries(state_dir, test_id, "result"):
        raise ValueError(f"{test_id} already has a verdict; verdicts are never edited")
    entries = ledger(state_dir)
    family = reg[-1]["protocol"]["family"]
    return _append(state_dir, {"id": test_id, "kind": "result", "at": _now(), "verdict": verdict,
                               "numbers": numbers, "note": note, "trials_total": len(trial_ids(entries)),
                               "trials_in_family": len(trial_ids(entries, family))})


def passed(state_dir, test_id: str) -> bool:
    return any(e.get("verdict") == "pass" for e in _entries(state_dir, test_id, "result"))


def status(state_dir) -> list[dict]:
    """One line per test in the ledger: registered, run, verdict, trial numbers."""
    entries = ledger(state_dir)
    out = []
    for tid in trial_ids(entries):
        mine = [e for e in entries if e.get("id") == tid]
        reg = next((e for e in mine if e.get("kind") == "registration"), {})
        res = next((e for e in mine if e.get("kind") == "result" or ("pass" in e and e.get("kind") is None)), {})
        verdict = res.get("verdict") or ({True: "pass", False: "fail"}.get(res.get("pass")) if res else None)
        out.append({"id": tid, "family": (reg.get("protocol") or {}).get("family", "-"),
                    "registered": reg.get("at") or "-", "ran": any(e.get("kind") == "run" for e in mine) or bool(res),
                    "verdict": verdict or "-"})
    return out


def format_status(state_dir) -> str:
    rows = status(state_dir)
    if not rows:
        return "No tests in the ledger yet."
    lines = [f"Tests in the ledger: {len(rows)} trials so far (every one counts, failures included)"]
    for r in rows:
        lines.append(f"  {r['id']:<40} {r['family']:<12} registered {r['registered']:<20} "
                     f"{'ran' if r['ran'] else 'not run':<8} verdict {r['verdict']}")
    return "\n".join(lines)
