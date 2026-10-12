"""Gate 1, deploy: a commit reaches the running bot (~/tradebot) when every check passes.

1. the agent checkout is clean and HEAD is the commit to deploy; the bot's checkout is a clean
   repository, and the container says which commit it was built from (TRADEBOT_COMMIT; the
   checkout's HEAD when it doesn't);
2. lint (`ruff check tradebot tests`);
3. the full test suite;
4. the test suite again in a fresh clone of that commit;
5. the same-trades check in that clone: the live path's orders on the fixed dataset equal the
   golden file;
6. if those orders differ from the golden file of the commit running now, the new golden file
   must name a registered test that passed (``accepted_by``), one that didn't already accept
   the running commit's orders;
7. deploy: fast-forward the bot's checkout to the commit, build the image (a failed build puts
   the checkout back), restart the container, watch it stay up for SETTLE_S seconds without a
   restart, and append the deploy to ``<state_dir>/deploys.jsonl``.

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
SETTLE_S = 45  # the container must stay up this long after the restart, without restarting


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


def running_commit(bot_dir, docker: tuple[str, ...] = ("docker",)) -> str | None:
    """The commit the container was built from (TRADEBOT_COMMIT), when it says."""
    try:
        ids = subprocess.run([*docker, "compose", "ps", "-q"], cwd=bot_dir, capture_output=True, text=True,
                             timeout=60).stdout.split()
        if not ids:
            return None
        env = subprocess.run([*docker, "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", ids[0]],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in env.splitlines():
        if line.startswith("TRADEBOT_COMMIT="):
            value = line.split("=", 1)[1].strip()
            return value if value and value != "unknown" else None
    return None


def golden_of(repo, commit: str) -> dict | None:
    """The golden file as committed at ``commit`` in ``repo``; None when that commit has none."""
    _git(repo, "rev-parse", "--git-dir", step="bot checkout")  # a repository, or the gate fails here
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{GOLDEN_REL}"], capture_output=True, text=True)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def golden_change_ok(new_golden: Path, bot_dir, state_dir, running: str = "HEAD") -> str:
    """Different orders than the commit running now are allowed only for a passed registered test, and a
    test accepts one change: not the one that already accepted the running commit's orders."""
    from ..protocols import passed

    old = golden_of(bot_dir, running)
    new = json.loads(new_golden.read_text())
    if old is None:
        return f"no golden file in the running commit ({running[:7]}) yet: this one becomes the reference"
    if new["orders"] == old.get("orders"):
        return "same orders as the running commit: not trade-affecting"
    by = new.get("accepted_by")
    if by and by == old.get("accepted_by"):
        raise GateFailed("same-trades", f"the orders differ from the running commit, and {by} already accepted the "
                                        f"running commit's orders: a registered test accepts one change")
    if by and passed(state_dir, by):
        return f"orders differ from the running commit ({running[:7]}), accepted by {by}, a registered test that passed"
    raise GateFailed("same-trades", "the orders differ from the running commit and "
                     + (f"{by} has no passing verdict in the ledger" if by else "no passed registered test accepts them"))


def deploy(agent_dir, bot_dir, state_dir, *, dry_run: bool = False, run_checks: bool = True,
           docker: tuple[str, ...] = ("docker",), log=print, settle_s: float = SETTLE_S) -> dict:
    agent_dir, bot_dir = Path(agent_dir).resolve(), Path(bot_dir).resolve()
    py = sys.executable
    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "steps": []}

    def step(name: str, detail: str = "ok"):
        record["steps"].append(f"{name}: {detail}")
        log(f"  [ok] {name}: {detail}")

    # the cheap checks first: both checkouts, before the minutes of tests
    if _git(agent_dir, "status", "--porcelain", "--untracked-files=no", step="clean checkout"):
        raise GateFailed("clean checkout", "the agent checkout has uncommitted changes: commit first")
    sha = _git(agent_dir, "rev-parse", "HEAD")
    record["commit"] = sha[:7]
    step("clean checkout", f"HEAD {sha[:7]}")
    before = _git(bot_dir, "rev-parse", "HEAD", step="bot checkout")
    if _git(bot_dir, "status", "--porcelain", "--untracked-files=no", step="bot checkout"):
        raise GateFailed("bot checkout", f"{bot_dir} has local changes to tracked files")
    running = running_commit(bot_dir, docker) or before
    record["previous"] = before[:7]
    step("bot checkout", f"HEAD {before[:7]}, the container runs "
                         + (running[:7] if running != before else "that commit"))
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
        step("orders vs the running commit", golden_change_ok(clone / GOLDEN_REL, bot_dir, state_dir, running))
    if dry_run:
        step("deploy", f"dry run: would fast-forward {bot_dir} to {sha[:7]} and rebuild")
        record["deployed"] = False
        return record
    _git(bot_dir, "fetch", "-q", str(agent_dir), sha, step="fetch into the bot checkout")
    _git(bot_dir, "merge", "--ff-only", "-q", sha, step="fast-forward the bot checkout")
    step("bot checkout", f"{before[:7]} -> {sha[:7]}")
    env = {**os.environ, "GIT_COMMIT": sha[:7], "TRADEBOT_UID": str(os.getuid()), "TRADEBOT_GID": str(os.getgid())}
    try:
        _run([*docker, "compose", "build"], cwd=bot_dir, env=env, step="docker compose build")
    except GateFailed:  # the container keeps running the old code: put the checkout back on it
        _git(bot_dir, "reset", "-q", "--hard", before, step="restore the bot checkout")
        raise
    step("docker compose build")
    _run([*docker, "compose", "up", "-d"], cwd=bot_dir, env=env, step="docker compose up")
    deadline = time.time() + settle_s
    while True:  # a bot that dies on start-up restarts (restart: unless-stopped): watch it settle
        ids = _run([*docker, "compose", "ps", "--status", "running", "-q"], cwd=bot_dir, env=env,
                   step="container check").split()
        if not ids:
            raise GateFailed("container check", "the container is not running after the rebuild")
        if time.time() >= deadline:
            break
        time.sleep(min(5.0, max(deadline - time.time(), 0.01)))
    restarts = subprocess.run([*docker, "inspect", "--format", "{{.RestartCount}}", ids[0]], capture_output=True,
                              text=True).stdout.strip()
    if restarts not in ("", "0"):
        raise GateFailed("container check", f"the container restarted {restarts} time(s) within {settle_s:.0f}s of "
                                            f"the rebuild: the new code doesn't start")
    step("container", f"running {settle_s:.0f}s without a restart")
    record["deployed"] = True
    with open(Path(state_dir) / "deploys.jsonl", "a") as fh:
        fh.write(json.dumps(record) + "\n")
    return record


def have_docker() -> bool:
    return shutil.which("docker") is not None
