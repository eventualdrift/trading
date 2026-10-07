"""What produced a result: the code version and the settings, so a saved run can be
reproduced - and a rerun under different code or settings is flagged as such."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .config import BotConfig

# the settings that change backtest results (secrets, Telegram, dashboard, paths excluded)
SECTIONS = ("timeframes", "strategies", "allow_short", "universe", "data", "costs", "risk", "core",
            "context", "guards", "selection", "ml")


CODE_PATHS = ("tradebot", "pyproject.toml", "requirements.txt")  # docs and notes (STATUS.md ...) don't count


def code_version() -> str:
    """Short git commit of the running code, '+uncommitted' if tracked code files were edited.
    In a docker image (no git inside) it is the commit the image was built from (TRADEBOT_COMMIT)."""
    import os

    baked = os.environ.get("TRADEBOT_COMMIT", "").strip()
    if baked and baked != "unknown":
        return baked
    repo = Path(__file__).resolve().parent.parent
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True,
                              text=True, timeout=5)
        if head.returncode != 0 or not head.stdout.strip():
            raise RuntimeError(head.stderr)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", *CODE_PATHS], cwd=repo,
                               capture_output=True, text=True, timeout=5)
        return head.stdout.strip() + ("+uncommitted" if dirty.stdout.strip() else "")
    except Exception:
        return f"unknown (tradebot {__version__}, not a git checkout)"


def config_snapshot(cfg: BotConfig) -> dict:
    d = asdict(cfg)
    snap = {k: d[k] for k in SECTIONS}
    snap["universe"] = {k: v for k, v in snap["universe"].items() if k != "refresh_hours"}
    # the timeframes a run used, shortest first: the same set always gives the same hash
    snap["timeframes"] = sorted(dict.fromkeys(snap["timeframes"]), key=_tf_order)
    return snap


def _tf_order(tf: str) -> tuple:
    from .timeframes import tf_ms

    try:
        return (tf_ms(tf), tf)
    except Exception:
        return (float("inf"), tf)


def settings_snapshot(cfg: BotConfig) -> dict:
    """The settings as loaded from the config file. Every report hashes these, whatever a command
    then adjusts for its run (the timeframes a selection uses, a --core-fraction): one hash for
    one config file. Reports print the run's own timeframes and core fraction beside it."""
    return cfg.__dict__.get("_settings_at_load") or config_snapshot(cfg)


def settings_hash(cfg: BotConfig) -> str:
    return config_hash(settings_snapshot(cfg))


def without_timeframes(snapshot: dict | None) -> dict | None:
    """Universe files saved before 2026-10-07 recorded the timeframes their run used in place of
    the config file's: compare those without them."""
    return None if snapshot is None else {k: v for k, v in snapshot.items() if k != "timeframes"}


def config_hash(snapshot: dict) -> str:
    return hashlib.sha1(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()[:10]


def _flat(d, prefix: str = "") -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, dict) and v:
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def config_differences(saved: dict, current: dict) -> list[str]:
    a, b = _flat(saved), _flat(current)
    return [f"{k}: saved {a.get(k)!r}, now {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


def config_summary(snapshot: dict) -> str:
    c, r, core, sel, u = (snapshot.get(k, {}) for k in ("costs", "risk", "core", "selection", "universe"))
    maker = c.get("maker_fee_rate")
    slots = (f"open-risk budget {r['max_open_risk_pct']:g}%" if r.get("max_open_risk_pct") is not None
             else f"max {r.get('max_open_positions')} open")
    return (f"fees {c.get('fee_rate', 0):.3%} taker / {(maker if maker is not None else c.get('fee_rate', 0)):.3%} maker, "
            f"slippage {c.get('slippage_rate', 0):.3%}, {c.get('entry_order')} entries; "
            f"risk {r.get('risk_per_trade_pct')}% per trade, {slots}, max {r.get('max_position_pct')}% per position; "
            f"core {core.get('fraction')} of {'+'.join(s.split('/')[0] for s in core.get('symbols', []))}, "
            f"reset every {core.get('rebalance_sleeves_days')} days; btc_filter {sel.get('btc_filter')}; "
            f"universe top {u.get('top_n')}, listed >= {u.get('min_history_days')} days; "
            f"learn's timeframes {', '.join(snapshot.get('timeframes', []))}")
