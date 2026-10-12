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

REQUIRED = ("id", "family", "hypothesis", "change", "data", "untouched", "reserve", "metric", "pass_rule", "readings",
            "run")
VERDICTS = ("pass", "fail", "inconclusive")
PER_QUARTER = 3  # at most this many protocols registered in a calendar quarter (CLAUDE.md, gate 2)
HALVES = ("overall", "first_half", "second_half")  # a pass holds on each half of the untouched period, and overall
# The family floors from CLAUDE.md (a protocol may be stricter, never looser): the metric, changed minus
# current, at least +0.10 for satellite / core / ml; the drawdown range check, a share, under 5%.
FLOORS = {"satellite": ("at least", 0.10), "core": ("at least", 0.10), "ml": ("at least", 0.10),
          "drawdown": ("under", 0.05)}
# Registrations from before the template (no reserve field) and the untouched data they judged a change on
LEGACY_SPENT = {"core-btc-pre2017-v1": "pre-2017 BTC"}


def _norm(x) -> str:
    return " ".join(str(x or "").split()).lower()


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


def quarter(at: str) -> str:
    """'2026-10-12 ...' -> '2026Q4'."""
    y, m = int(at[:4]), int(at[5:7])
    return f"{y}Q{(m - 1) // 3 + 1}"


def validate(proto: dict) -> list[str]:
    problems = [f"missing '{k}'" for k in REQUIRED
                if not proto.get(k) or (isinstance(proto.get(k), str) and not proto[k].strip())]
    readings = proto.get("readings") or {}
    if not isinstance(readings, dict) or not readings.get("pass") or not readings.get("fail"):
        problems.append("readings needs at least 'pass' and 'fail'")
    if proto.get("run") and not str(proto["run"]).startswith("tradebot "):
        problems.append("run must be a single `tradebot ...` command")
    if proto.get("pass_rule") and "half" not in str(proto["pass_rule"]).lower():
        problems.append("pass_rule must say that the pass holds on each half of the untouched period separately as "
                        "well as overall (the run prints the metric overall and per half)")
    return problems


def register(state_dir, proto: dict) -> dict:
    problems = validate(proto)
    if problems:
        raise ValueError("protocol not registered: " + "; ".join(problems))
    entries = ledger(state_dir)
    if any(e.get("id") == proto["id"] for e in entries):
        raise ValueError(f"{proto['id']} is already in the ledger: a second look needs a new id (and counts as a trial)")
    mine = _norm(proto["reserve"])
    for e in entries:  # a reserve is spent once: the same name, or one containing the other, is refused
        if e.get("kind") != "registration":
            continue
        theirs = _norm((e.get("protocol") or {}).get("reserve") or LEGACY_SPENT.get(e.get("id"), ""))
        if theirs and (theirs == mine or theirs in mine or mine in theirs):
            raise ValueError(f"the untouched reserve {proto['reserve']!r} was spent by {e['id']} ({theirs!r}): data a "
                             f"protocol has judged a change on is not untouched any more; name another reserve")
    now = _now()
    this_quarter = [e for e in entries if e.get("kind") == "registration" and quarter(str(e.get("at", ""))) == quarter(now)]
    if len(this_quarter) >= PER_QUARTER:
        raise ValueError(f"{PER_QUARTER} protocols are already registered in {quarter(now)} "
                         f"({', '.join(e['id'] for e in this_quarter)}): at most {PER_QUARTER} per quarter")
    canon = json.dumps(proto, sort_keys=True)
    entry = {"id": proto["id"], "kind": "registration", "at": now, "protocol": proto,
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
    family = reg[-1]["protocol"]["family"]
    if verdict == "pass":
        vals = {k: numbers.get(k) for k in HALVES}
        if any(v is None or isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals.values()):
            raise ValueError(f"a pass needs the metric {', '.join(HALVES)} in --numbers, each a number: it must hold on "
                             f"each half of the untouched period separately as well as overall")
        side, floor = FLOORS.get(family, (None, None))
        short = [k for k, v in vals.items() if (side == "at least" and v < floor) or (side == "under" and v >= floor)]
        if short:
            raise ValueError(f"not a pass for family {family}: {', '.join(f'{k}={vals[k]}' for k in short)} "
                             f"({side} {floor} is the family floor, on each half and overall)")
    entries = ledger(state_dir)
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
