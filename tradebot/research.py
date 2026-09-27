"""Research helpers: evaluate an optional rule on real data before switching it on."""
from __future__ import annotations

from dataclasses import dataclass

from .backtest.metrics import trade_metrics
from .backtest.selection import Selection, run_combo
from .config import BotConfig


@dataclass
class BreakerRow:
    combo: str
    ratio: float | None  # None = breaker off
    is_trades: int
    is_exp: float
    oos_trades: int
    oos_exp: float


def breaker_study(selection: Selection, datasets_by_tf: dict, cfg: BotConfig, context,
                  ratios=(2.0, 2.5, 3.0)) -> list[BreakerRow]:
    """Backtest each selected combo with the volatility breaker off and at each ratio."""
    rows = []
    for c in selection.selected:
        if c.timeframe not in datasets_by_tf:
            continue
        for ratio in (None, *ratios):
            is_t, oos_t, _ = run_combo(datasets_by_tf[c.timeframe], c.strategy, c.params, c.timeframe,
                                       cfg, context, vol_breaker=ratio)
            mi = trade_metrics([t.r_multiple for t in is_t])
            mo = trade_metrics([t.r_multiple for t in oos_t])
            rows.append(BreakerRow(c.key, ratio, mi["trades"], mi["expectancy_r"], mo["trades"], mo["expectancy_r"]))
    return rows


def format_breaker_study(rows: list[BreakerRow]) -> str:
    out = [f"{'combo':<16}{'breaker':>9}{'IS trades':>11}{'IS exp':>9}{'OOS trades':>12}{'OOS exp':>9}  verdict"]
    base = {}
    helps_everywhere = {}
    for r in rows:
        if r.ratio is None:
            base[r.combo] = r
            verdict = "(baseline)"
        else:
            b = base[r.combo]
            better = r.is_exp > b.is_exp and r.oos_exp > b.oos_exp
            helps_everywhere.setdefault(r.ratio, True)
            helps_everywhere[r.ratio] &= better
            verdict = "helps in both periods" if better else "does not help"
        label = "off" if r.ratio is None else f"{r.ratio:g}"
        out.append(f"{r.combo:<16}{label:>9}{r.is_trades:>11}{r.is_exp:>+9.3f}{r.oos_trades:>12}{r.oos_exp:>+9.3f}  {verdict}")
    good = [ratio for ratio, ok in helps_everywhere.items() if ok]
    out.append("")
    if good:
        out.append(f"Recommendation: the breaker improved every selected strategy in and out of sample at ratio(s) "
                   f"{', '.join(f'{g:g}' for g in good)}. To enable: guards.vol_breaker: true, "
                   f"guards.vol_breaker_ratio: {max(good):g}")
    else:
        out.append("Recommendation: keep the breaker OFF - it did not improve every selected strategy in both periods.")
    return "\n".join(out)


# ------------------------------------------------------------------ sizing (pre-registered)
# Fixed before any real-data run (item-7 rules): one rule, one criterion, one run.
SIZING_TEST = {
    "id": "sizing-open-risk-budget-v1",
    "registered": "2026-09-27",
    "rule": ("Replace 'max 3 open positions' with an open-risk budget of 3% of satellite equity. A trade's "
             "risk at stake = size x distance to its current stop, 0 once the stop is at or past entry. "
             "Unchanged: 1% risk per trade, max 30% per position, one per coin, total <= 100% of the satellite."),
    "reason": ("The position count treats trades whose stop is at breakeven (they can no longer lose) like fresh "
               "ones, so protected winners block new signals. 3% keeps today's worst case (3 fresh trades x 1%)."),
    "criterion": ("PASS if, from 2022-01-01, the 65/35 account with the rule has a Sharpe at least 0.10 above "
                  "core-only, OR a worst drawdown at least 3 points smaller than the core held at the same "
                  "average exposure (rest in cash). Full history is reported alongside. One run."),
    "budget_pct": 3.0,
    "since": "2022-01-01",
    "sharpe_margin": 0.10,
    "dd_margin_pts": 3.0,
    "variants_tested": 1,
}


def sizing_verdict(stats: dict, test: dict = SIZING_TEST) -> dict:
    """Apply the pre-registered criterion to account_stats() of the test period."""
    comb, core, matched = stats["combined"], stats["core"], stats["matched"]
    sharpe_ok = comb["sharpe"] >= core["sharpe"] + test["sharpe_margin"]
    dd_ok = comb["max_dd_pct"] <= matched["max_dd_pct"] - test["dd_margin_pts"]
    worded = comb["sharpe"] > core["sharpe"] or comb["max_dd_pct"] < matched["max_dd_pct"]
    return {"pass": bool(sharpe_ok or dd_ok), "sharpe_ok": bool(sharpe_ok), "dd_ok": bool(dd_ok),
            "pass_without_margins": bool(worded)}


def sizing_study(core_closes, trades, cfg: BotConfig, *, capital: float, fraction: float, sat_closes=None,
                 combos=None, universe=None, test: dict = SIZING_TEST) -> dict:
    import pandas as pd

    from .portfolio import account_stats, portfolio_backtest

    since = pd.Timestamp(test["since"], tz="UTC")
    runs = {}
    for label, budget in (("baseline", None), ("rule", test["budget_pct"])):
        res = portfolio_backtest(core_closes, trades, cfg, capital=capital, fraction=fraction, since=test["since"],
                                 sat_closes=sat_closes, combos=combos, universe=universe, open_risk_pct=budget)
        sat = res.satellite
        runs[label] = {
            "test": account_stats(res, since), "full": account_stats(res, None),
            "taken": len(sat.taken) if sat else 0,
            "skipped": {k: len(v) for k, v in (sat.skipped.items() if sat else [])},
            "sizing": res.sizing,
        }
    u = universe or {}
    return {"test": test, "fraction": fraction, "capital": capital, "candidates": len(trades),
            "assumptions": cfg.costs_description(), "runs": runs,
            "universe": {"symbols": u.get("symbols"), "data_end_ms": u.get("data_end_ms"), "file": u.get("file"),
                         "source": u.get("source")},
            "verdict": sizing_verdict(runs["rule"]["test"], test),
            "baseline_verdict": sizing_verdict(runs["baseline"]["test"], test)}


def format_sizing_study(study: dict) -> str:
    t = study["test"]
    lines = [f"Pre-registered sizing test {t['id']} (registered {t['registered']}; variants tested: "
             f"{t['variants_tested']} + today's rule as the baseline)",
             f"  Rule: {t['rule']}", f"  Why: {t['reason']}", f"  Pass criterion: {t['criterion']}",
             f"  {study['assumptions']}", f"  {study['candidates']} candidate satellite trades; account "
             f"{study['fraction']:.0%} core / {1 - study['fraction']:.0%} satellite"]
    for period, title in (("test", f"Test period (from {t['since']})"), ("full", "Full history")):
        lines += ["", f"{title}:",
                  f"  {'':<34}{'per year':>10}{'worst dip':>11}{'Sharpe':>8}{'exposure':>10}"]
        for label, run in study["runs"].items():
            s = run[period]
            name = "today's rule (3 positions)" if label == "baseline" else f"open-risk budget {t['budget_pct']:g}%"
            rows = [(f"65/35, {name}", s["combined"]),
                    (f"  core at same exposure ({s['matched']['k']:.0%})", s["matched"]),
                    ("  satellite only", s["satellite"])]
            if label == "baseline":
                rows.insert(0, ("Core only", s["core"]))
            for rname, st in rows:
                ex = f"{st['exposure']:>9.0%}" if "exposure" in st else f"{'-':>9}"
                lines.append(f"  {rname:<34}{st['cagr_pct']:>+9.1f}%{-st['max_dd_pct']:>10.0f}%"
                             f"{st['sharpe']:>8.2f} {ex}")
    lines.append("")
    for label, run in study["runs"].items():
        skipped = ", ".join(f"{v} {k}" for k, v in run["skipped"].items()) or "none"
        lines.append(f"Satellite, {'today' if label == 'baseline' else 'with the rule'}: {run['taken']} of "
                     f"{study['candidates']} candidates taken; skipped: {skipped}")
    v, rule_test = study["verdict"], study["runs"]["rule"]["test"]
    lines += ["",
              f"Sharpe: 65/35 with the rule {rule_test['combined']['sharpe']:.2f} vs core-only "
              f"{rule_test['core']['sharpe']:.2f} (needs +{t['sharpe_margin']:.2f}): {'yes' if v['sharpe_ok'] else 'no'}",
              f"Worst dip: {-rule_test['combined']['max_dd_pct']:.1f}% vs core at same exposure "
              f"{-rule_test['matched']['max_dd_pct']:.1f}% (needs {t['dd_margin_pts']:g} points smaller): "
              f"{'yes' if v['dd_ok'] else 'no'}",
              f"(Today's rule on the same test: {'passes' if study['baseline_verdict']['pass'] else 'fails'}; "
              f"without the margins the rule would {'pass' if v['pass_without_margins'] else 'fail'}.)",
              *(["Note: today's rule passes this criterion too, so the test does not show the new rule is better "
                 "than today's - compare their rows above."] if study["baseline_verdict"]["pass"] else []),
              "",
              f"VERDICT: {'PASS - the rule may be enabled for paper trading (risk.max_open_risk_pct: ' + format(t['budget_pct'], 'g') + ')' if v['pass'] else 'FAIL - keep today’s rule; do not retry variants of this rule on the same data'}"]
    return "\n".join(lines)


def record_study(state_dir, study: dict, rerun: bool = False):
    """Keep the result and append to the research ledger (every test ever run, for the variant count)."""
    import json
    import time
    from pathlib import Path

    folder = Path(state_dir) / "research"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{study['test']['id']}.json"
    if not rerun:
        path.write_text(json.dumps({**study, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2, default=str))
    with open(folder / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps({"id": study["test"]["id"], "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "variants_tested": study["test"]["variants_tested"], "rerun": rerun,
                             "pass": study["verdict"]["pass"],
                             "universe_file": (study.get("universe") or {}).get("file"),
                             "data_end_ms": (study.get("universe") or {}).get("data_end_ms")}) + "\n")
    return path


def previous_result(state_dir, test: dict = SIZING_TEST) -> dict | None:
    import json
    from pathlib import Path

    path = Path(state_dir) / "research" / f"{test['id']}.json"
    return json.loads(path.read_text()) if path.exists() else None


def add_note(state_dir, test_id: str, text: str) -> dict:
    """Annotate a recorded test without changing it: appended to the ledger and to the result's
    notes (its numbers and verdict are never edited)."""
    import json
    import time
    from pathlib import Path

    folder = Path(state_dir) / "research"
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"id": test_id, "kind": "note", "at": time.strftime("%Y-%m-%d %H:%M:%S"), "note": text}
    with open(folder / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps(entry) + "\n")
    result = folder / f"{test_id}.json"
    if result.exists():
        data = json.loads(result.read_text())
        data.setdefault("notes", []).append({"at": entry["at"], "note": text})
        result.write_text(json.dumps(data, indent=2, default=str))
    return entry
