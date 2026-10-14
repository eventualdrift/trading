"""What a fidelity fix does to learn's strategy selection (CLAUDE.md, working rules).

A fidelity fix applies to every use of the backtest, learn included. A selection change it causes
is recorded in the ledger and leads the Monday summary; it is not a stop-and-ask. `tradebot
research fidelity --fix <commit>` runs learn's selection on the weekly reference's frozen data
(its coins and data end; candles the cache lacks are fetched, never saved) twice, with the code
just before the fix and with the fix, each from a clean copy of its commit, so nothing else that
changed since counts. The difference goes in the ledger (kind "selection"), once per fix, and the
first `tradebot weekly` after the fix is deployed leads its summary with a change. Not a trial:
nothing is chosen on it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent

# Runs in the copy of a commit, so it uses only what every recent commit has.
PROBE = r"""
import json, sys
import tradebot
from tradebot.backtest.selection import run_selection
from tradebot.config import load_config
from tradebot.data import ExchangeClient, OHLCVStore
from tradebot.learning import load_context, load_datasets

config, universe, data_dir, fetch, out = sys.argv[1:6]
cfg = load_config(config, env_file=None)
cfg.data.dir = data_dir
ref = json.load(open(universe))
store = OHLCVStore(cfg.data.dir, cfg.exchange.data_exchange_id or cfg.exchange.id)
client = (ExchangeClient(cfg.exchange.data_exchange_id or cfg.exchange.id, market_type=cfg.exchange.market_type)
          if fetch == "1" else None)  # fills what the cache lacks up to the data end; the cache is never written
quiet = lambda *a: None
end = ref["data_end_ms"]
datasets = load_datasets(client, cfg, list(ref["symbols"]), store, log_fn=quiet, end_ms=end)
context = load_context(client, cfg, store, log_fn=quiet, end_ms=end)
sel = run_selection(datasets, cfg, log=quiet, context=context)
json.dump({
    "code_file": tradebot.__file__,
    "data": {tf: {s: [len(d), str(d.index[0]), str(d.index[-1])] for s, d in sorted(ds.items())}
             for tf, ds in sorted(datasets.items())},
    "context": context is not None,
    "combos": [{"key": c.key, "params": c.params, "selected": bool(c.selected),
                "in_sample": [c.in_sample.get("trades"), c.in_sample.get("expectancy_r")],
                "out_of_sample": [c.out_of_sample.get("trades"), c.out_of_sample.get("expectancy_r")],
                "reasons": list(c.reasons)} for c in sel.combos],
}, open(out, "w"), default=str)
"""


def _git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if done.returncode != 0:
        raise ValueError(f"git {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def _extract(commit: str, dest: Path) -> None:
    """The commit's files, no git state."""
    archive = subprocess.run(["git", "archive", commit], cwd=REPO, capture_output=True)
    if archive.returncode != 0:
        raise RuntimeError(f"git archive {commit[:7]}: {archive.stderr.decode(errors='replace').strip()}")
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


def fix_commits(fix: str) -> tuple[str, str, str]:
    """(the commit before the fix, the fix, the fix's subject), full hashes."""
    after = _git("rev-parse", "--verify", f"{fix}^{{commit}}")
    before = _git("rev-parse", "--verify", f"{after}^")
    return before, after, _git("log", "-1", "--format=%s", after)


def measure(before: str, after: str, config, universe_file, data_dir, *, fetch: bool = True,
            timeout: float = 3600) -> dict:
    """Learn's selection on the frozen universe with each commit's code, run side by side."""
    paths = [str(Path(p).resolve()) for p in (config, universe_file, data_dir)]
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="tradebot-fidelity-") as tmp:
        procs = {}
        try:
            for side, commit in (("before", before), ("after", after)):
                code = Path(tmp) / side
                code.mkdir()
                _extract(commit, code)
                out = Path(tmp) / f"{side}.json"
                with open(Path(tmp) / f"{side}.log", "w") as log:
                    procs[side] = (subprocess.Popen(
                        [sys.executable, "-c", PROBE, *paths, "1" if fetch else "0", str(out)], cwd=code, stdout=log,
                        stderr=subprocess.STDOUT, env={**os.environ, "PYTHONPATH": str(code)}), code, out)
            runs = {}
            for side, (proc, code, out) in procs.items():
                try:
                    rc = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise RuntimeError(f"learn's selection ran past {timeout:.0f}s: stopped") from None
                if rc != 0:
                    tail = (Path(tmp) / f"{side}.log").read_text().strip()[-1500:]
                    raise RuntimeError(f"learn's selection with the code {side} the fix failed:\n{tail}")
                runs[side] = json.loads(out.read_text())
                if not Path(runs[side]["code_file"]).resolve().is_relative_to(code.resolve()):
                    raise RuntimeError(f"the {side} run imported {runs[side]['code_file']}, not its own copy")
        finally:
            for proc, *_ in procs.values():
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
    if (runs["before"]["data"], runs["before"]["context"]) != (runs["after"]["data"], runs["after"]["context"]):
        raise RuntimeError("the two runs read different candles: nothing to compare")
    ref = json.loads(Path(universe_file).read_text())
    return {**compare(runs["before"], runs["after"]),
            "coverage": coverage(runs["after"]["data"], ref["symbols"], ref["data_end_ms"])}


def label(c: dict) -> str:
    """A selected combination with its parameters: a filter switched on or off is another selection."""
    return c["key"] + (" " + json.dumps(c["params"], sort_keys=True) if c.get("params") else "")


def compare(before: dict, after: dict) -> dict:
    def chosen(run):
        return sorted(label(c) for c in run["combos"] if c["selected"])

    b, a = chosen(before), chosen(after)
    old, new = ({c["key"]: c for c in run["combos"]} for run in (before, after))
    rows = [{"key": k, "before": _row(old.get(k)), "after": _row(new.get(k))} for k in sorted(set(old) | set(new))]
    data = json.dumps([after["data"], after["context"]], sort_keys=True).encode()
    return {"before": b, "after": a, "added": sorted(set(a) - set(b)), "dropped": sorted(set(b) - set(a)),
            "rows": rows, "data_sha256": hashlib.sha256(data).hexdigest()[:16],
            "symbols": sorted({s for ds in after["data"].values() for s in ds})}


def coverage(data: dict, symbols, end_ms: int) -> dict:
    """Per timeframe: the reference's coins with no candles, and those whose candles stop before the
    data end (a candle that had closed by then is missing)."""
    from .timeframes import tf_ms

    out = {}
    for tf, ds in data.items():
        short = [s for s, (_, _, last) in ds.items()
                 if int(pd.Timestamp(last).value // 1_000_000) + 2 * tf_ms(tf) <= end_ms]
        out[tf] = {"coins": len(ds), "missing": sorted(set(symbols) - set(ds)), "ends_early": sorted(short)}
    return out


def _row(c: dict | None) -> dict | None:
    if c is None:
        return None
    return {"selected": c["selected"], "params": c.get("params") or {}, "in_sample": c["in_sample"],
            "out_of_sample": c["out_of_sample"], "reason": (c["reasons"] or [None])[0]}


def _r(x) -> str:
    return "-" if x is None else f"{x:+.3f}R"


def coverage_text(cov: dict) -> str:
    parts = []
    for tf, c in cov.items():
        gaps = ([f"no candles for {', '.join(c['missing'])}"] if c["missing"] else []) + \
               ([f"{len(c['ends_early'])} stop before the data end"] if c["ends_early"] else [])
        parts.append(f"{tf} {c['coins']}" + (f" ({'; '.join(gaps)})" if gaps else ""))
    return "coins per timeframe: " + ", ".join(parts)


def format_comparison(rec: dict) -> str:
    lines = [f"Learn's selection on the frozen reference {rec['universe_file']} (its coins and data end; settings "
             f"{rec['settings']}):",
             f"  {coverage_text(rec['coverage'])}" if rec.get("coverage") else f"  {len(rec['symbols'])} coins",
             f"  before the fix ({rec['before_code'][:7]}): {', '.join(rec['before']) or 'none'}",
             f"  with the fix   ({rec['fix'][:7]}): {', '.join(rec['after']) or 'none'}",
             "  Change: " + change_text(rec), "",
             f"  {'combination':<14} {'in-sample: trades R/trade, before -> with':<32}   "
             f"{'out-of-sample: the same':<32}"]
    header = len(lines)
    for r in rec["rows"]:
        b, a = r["before"] or {}, r["after"] or {}
        if b == a:
            continue
        cell = []
        for part in ("in_sample", "out_of_sample"):
            nb, eb = (b.get(part) or [None, None])
            na, ea = (a.get(part) or [None, None])
            cell.append(f"{nb if nb is not None else '-':>5} {_r(eb):>8} -> {na if na is not None else '-':>5} "
                        f"{_r(ea):>8}")
        mark = {(False, True): "  added", (True, False): "  dropped"}.get(
            (bool(b.get("selected")), bool(a.get("selected"))), "")
        if not mark and b.get("selected") and b.get("params") != a.get("params"):
            mark = f"  parameters {b.get('params')} -> {a.get('params')}"
        lines.append(f"  {r['key']:<14} {cell[0]}   {cell[1]}{mark}")
    if len(lines) == header:
        lines.append("  (every combination scores the same)")
    return "\n".join(lines)


def change_text(rec: dict) -> str:
    parts = ([f"added {', '.join(rec['added'])}"] if rec["added"] else []) + \
            ([f"dropped {', '.join(rec['dropped'])}"] if rec["dropped"] else [])
    return "; ".join(parts) or "the same strategies"


def entry_id(fix: str) -> str:
    return f"fidelity-{fix[:7]}"


def recorded(state_dir, fix: str) -> dict | None:
    from .protocols import ledger

    return next((e for e in ledger(state_dir) if e.get("kind") == "selection" and e.get("id") == entry_id(fix)), None)


def record(state_dir, rec: dict) -> dict:
    """Append the measurement to the ledger, once per fix."""
    from .protocols import _append, folder

    if recorded(state_dir, rec["fix"]):
        raise ValueError(f"{entry_id(rec['fix'])} is already in the ledger: one record per fix")
    folder(state_dir).mkdir(parents=True, exist_ok=True)
    return _append(state_dir, {"id": entry_id(rec["fix"]), "kind": "selection",
                               "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), **rec})


def unreported(state_dir, reported) -> list[dict]:
    """Every fix's record not yet in a check-in, changes or not."""
    from .protocols import ledger

    seen = set(reported or [])
    return [e for e in ledger(state_dir) if e.get("kind") == "selection" and e.get("id") not in seen]


def _contains(commit: str, fix: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", fix, commit], cwd=REPO,
                          capture_output=True).returncode == 0


def deployed_in(state_dir, fix: str) -> dict | None:
    """The first deploy (gate 1, state/deploys.jsonl) whose commit contains the fix, or None."""
    path = Path(state_dir) / "deploys.jsonl"
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        d = json.loads(line) if line.strip() else {}
        if d.get("deployed") and d.get("commit") and _contains(d["commit"], fix):
            return d
    return None


def lead_line(e: dict, deploy: dict) -> str:
    """The Monday summary's first line for a deployed fix that changes learn's selection."""
    return (f"fidelity fix {e['fix'][:7]} ({e['subject']}) changes learn's selection on the frozen reference: "
            f"{change_text(e)} (ledger {e['id']}; deployed {deploy['at'][:16]} UTC in {deploy['commit']}: learn picks "
            f"with it from its first weekly run after that)")
