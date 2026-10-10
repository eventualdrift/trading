"""Gate 1, deploy: a commit reaches the running bot (~/tradebot) when every check passes.

1. the agent checkout is clean and HEAD is the commit to deploy;
2. lint (`ruff check tradebot tests`);
3. the full test suite;
4. the test suite again in a fresh clone of that commit;
5. the same-trades check in that clone: the live path's orders on the fixed dataset equal the
   golden file;
6. if those orders differ from the golden file of the commit running now, the new golden file
   must name a registered test that passed (``accepted_by``);
7. deploy: fast-forward the bot's checkout to the commit, rebuild and restart the container,
   confirm it runs, and append the deploy to ``<state_dir>/deploys.jsonl``.

Any failure stops the gate before the bot is touched, and says which step failed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GOLDEN_REL = "tradebot/gates/golden.json"


class GateFailed(Exception):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


def _run(cmd: list[str], cwd, env=None, step: str = "", timeout: int = 3600) -> str:
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise GateFailed(step or cmd[0], out[-3000:] or f"exit code {proc.returncode}")
    return out


def _git(repo, *args, step="git") -> str:
    return _run(["git", "-C", str(repo), *args], cwd=repo, step=step)


def committed_orders(repo) -> dict | None:
    """The golden orders of the commit a checkout is on (not its working files)."""
    proc = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{GOLDEN_REL}"], capture_output=True, text=True)
    return json.loads(proc.stdout).get("orders") if proc.returncode == 0 else None


def golden_change_ok(new_golden: Path, bot_dir, state_dir) -> str:
    """Different orders than the commit running now are allowed only for a passed registered test."""
    from ..protocols import passed

    old = committed_orders(bot_dir)
    new = json.loads(new_golden.read_text())
    if old is None:
        return "no golden file in the running commit yet: this one becomes the reference"
    if new["orders"] == old:
        return "same orders as the running commit: not trade-affecting"
    by = new.get("accepted_by")
    if by and passed(state_dir, by):
        return f"orders differ from the running commit, accepted by {by}, a registered test that passed"
    raise GateFailed("same-trades", "the orders differ from the running commit and "
                     + (f"{by} has no passing verdict in the ledger" if by else "no passed registered test accepts them"))


def deploy(agent_dir, bot_dir, state_dir, *, dry_run: bool = False, run_checks: bool = True,
           docker: tuple[str, ...] = ("docker",), log=print) -> dict:
    agent_dir, bot_dir = Path(agent_dir).resolve(), Path(bot_dir).resolve()
    py = sys.executable
    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "steps": []}

    def step(name: str, detail: str = "ok"):
        record["steps"].append(f"{name}: {detail}")
        log(f"  [ok] {name}: {detail}")

    if _git(agent_dir, "status", "--porcelain", "--untracked-files=no", step="clean checkout"):
        raise GateFailed("clean checkout", "the agent checkout has uncommitted changes: commit first")
    sha = _git(agent_dir, "rev-parse", "HEAD")
    record["commit"] = sha[:7]
    step("clean checkout", f"HEAD {sha[:7]}")
    if run_checks:
        _run([py, "-m", "ruff", "check", "tradebot", "tests"], cwd=agent_dir, step="lint")
        step("lint")
        _run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=agent_dir, step="full suite")
        step("full suite")
    with tempfile.TemporaryDirectory() as tmp:
        clone = Path(tmp) / "clone"
        _run(["git", "clone", "-q", str(agent_dir), str(clone)], cwd=tmp, step="fresh clone")
        _git(clone, "checkout", "-q", sha, step="fresh clone")
        env = {**os.environ, "PYTHONPATH": str(clone)}
        if run_checks:
            _run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=clone, env=env, step="fresh-clone suite")
            step("fresh-clone suite")
            out = _run([py, "-m", "tradebot.cli", "--config", str(clone / "none.yaml"), "--env", str(clone / "none.env"),
                        "gate", "same-trades"], cwd=clone, env=env, step="same-trades")
            step("same-trades", out.splitlines()[-1] if out else "identical")
        step("orders vs the running commit", golden_change_ok(clone / GOLDEN_REL, bot_dir, state_dir))
    if dry_run:
        step("deploy", f"dry run: would fast-forward {bot_dir} to {sha[:7]} and rebuild")
        record["deployed"] = False
        return record
    before = _git(bot_dir, "rev-parse", "HEAD", step="bot checkout")
    if _git(bot_dir, "status", "--porcelain", "--untracked-files=no", step="bot checkout"):
        raise GateFailed("bot checkout", f"{bot_dir} has local changes to tracked files")
    _git(bot_dir, "fetch", "-q", str(agent_dir), sha, step="fetch into the bot checkout")
    _git(bot_dir, "merge", "--ff-only", "-q", sha, step="fast-forward the bot checkout")
    step("bot checkout", f"{before[:7]} -> {sha[:7]}")
    record["previous"] = before[:7]
    env = {**os.environ, "GIT_COMMIT": sha[:7], "TRADEBOT_UID": str(os.getuid()), "TRADEBOT_GID": str(os.getgid())}
    _run([*docker, "compose", "up", "-d", "--build"], cwd=bot_dir, env=env, step="docker compose up")
    running = _run([*docker, "compose", "ps", "--status", "running", "-q"], cwd=bot_dir, env=env, step="container check")
    if not running.strip():
        raise GateFailed("container check", "the container is not running after the rebuild")
    step("container", "running")
    record["deployed"] = True
    with open(Path(state_dir) / "deploys.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def have_docker() -> bool:
    return shutil.which("docker") is not None
