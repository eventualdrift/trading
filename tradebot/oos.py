"""Out-of-sample test of the core's rule on BTC before our Binance data (run once).

Order, enforced: ``fetch`` (data + a quality summary, no rule results) -> ``register`` (the
protocol and the data's fingerprint go into the research ledger) -> ``run`` (only if registered,
only on the registered data, only once) -> ``show``.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest.engine import Costs
from .config import CoreConfig
from .core import simulate_core_detail

DAY = pd.Timedelta(days=1)
END = pd.Timestamp("2017-08-16", tz="UTC")  # the day before our Binance data starts (inclusive)
FETCH_FROM = pd.Timestamp("2011-01-01", tz="UTC")  # before Bitstamp's first candle

TEST = {
    "id": "core-btc-pre2017-v1",
    "question": "Does the live core rule hold up on BTC history our backtests never used?",
    "data": ("Bitstamp BTC/USD daily candles (UTC) from the first available day to 2017-08-16 inclusive, the day "
             "before our Binance data starts. The raw file's SHA-256 is recorded at registration; the run refuses "
             "any other file."),
    "cleaning": [
        "Only daily closes are used (the rule acts on the daily close).",
        "The calendar is every UTC day from the first day with a positive close to 2017-08-16.",
        "A day is FILLED (takes the previous valid close, i.e. a flat day) if: it has no candle; its volume is 0; "
        "its close is missing or not positive; or it is a bad print - its close is at least 1.5x or at most 1/1.5 "
        "of the previous valid close AND the next day's close is within 10% of that previous valid close (if the "
        "next day has no close, the move is kept).",
        "If more than 5% of the trading period's days are filled, the data is unusable: no verdict is given.",
    ],
    "rule": {"description": "the live core rule unchanged, BTC only, 100% of a test account",
             "sma_days": [50, 100, 150, 200], "steps": "25% (share of the four averages the close is above)",
             "drift_tolerance": 0.2, "min_trade_usd": 10.0, "account_usd": 1000.0},
    "warmup": ("The first 200 days are warm-up. The rule and buy-and-hold both start with the full account in cash "
               "at the close of day 199 and can first trade at the close of day 200 (the first close with a 200-day "
               "average), paying 0.5% on entry. Returns are measured from the close of day 199 to the close of "
               "2017-08-16."),
    "costs": "0.5% per side, all-in (fee 0.5%, no separate slippage).",
    "benchmark": ("Buy-and-hold BTC over the same period: bought at the close of day 200 paying 0.5%, held to "
                  "2017-08-16."),
    "metrics": "Sharpe and worst dip as in portfolio-backtest (daily returns, Sharpe x sqrt(365); peak-to-low dip).",
    "pass": "Sharpe at least equal to buy-and-hold's AND a shallower worst dip.",
    "reading": {
        "pass": "PASS: out-of-sample support for the core's rule.",
        "dip_only": "Shallower dip but lower Sharpe: drawdown control holds; no evidence of a better risk-adjusted "
                    "return.",
        "fail": "Dip not shallower: the rule failed its main job; the core's size gets reconsidered.",
    },
    "runs": "Once. No variants, no reruns with other settings.",
}
FEE = 0.005


def data_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "oos" / "bitstamp_BTC-USD_1d_to_2017-08-16.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def fetch(client, data_dir) -> Path:
    """Raw candles exactly as the exchange returns them (no cleaning), saved for fingerprinting."""
    end_ms = int((END + DAY).value // 1_000_000)
    df = client.history("BTC/USD", "1d", int(FETCH_FROM.value // 1_000_000), end_ms)
    if df.empty:
        raise RuntimeError("the exchange returned no BTC/USD daily candles for 2011-2017")
    df = df[df.index <= END]
    path = data_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[["open", "high", "low", "close", "volume"]].copy()
    out.index = (out.index.as_unit("ns").asi8 // 1_000_000).astype(np.int64)
    out.index.name = "ts_ms"
    out.to_csv(path, float_format="%.10g")
    return path


def load_raw(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, index_col=0)
    df.index = pd.to_datetime(df.index.astype(np.int64), unit="ms", utc=True).as_unit("ns")
    return df.astype(float).sort_index()


def clean(raw: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """Apply the registered cleaning rule -> (daily closes, one row per filled day with the reason)."""
    valid = raw[(raw["close"] > 0) & raw["close"].notna()]
    if valid.empty:
        raise ValueError("no positive closes")
    days = pd.date_range(valid.index[0].floor("D"), END, freq="D", tz="UTC").as_unit("ns")
    day_close = raw["close"].groupby(raw.index.floor("D")).last().reindex(days)
    day_vol = raw["volume"].groupby(raw.index.floor("D")).last().reindex(days)
    raw_close = day_close.to_numpy(dtype=float)
    out, filled = np.empty(len(days)), []
    prev = float("nan")
    for i, d in enumerate(days):
        c, v = raw_close[i], day_vol.iloc[i]
        reason = None
        if c != c:
            reason = "no candle"
        elif c <= 0:
            reason = "close not positive"
        elif not v > 0:
            reason = "zero volume"
        elif prev == prev and (c >= 1.5 * prev or c <= prev / 1.5):
            nxt = raw_close[i + 1] if i + 1 < len(days) else float("nan")
            if nxt == nxt and abs(nxt / prev - 1) <= 0.10:
                reason = "bad print (spike reversed next day)"
        if reason is None:
            prev = c
            out[i] = c
        else:
            out[i] = prev
            filled.append({"day": d, "reason": reason, "raw_close": c, "used": prev,
                           "next_close": raw_close[i + 1] if i + 1 < len(days) else float("nan")})
    closes = pd.Series(out, index=days).dropna()  # leading days before any valid close cannot occur
    return closes, pd.DataFrame(filled, columns=["day", "reason", "raw_close", "used", "next_close"])


def trading_start(closes: pd.Series) -> pd.Timestamp:
    longest = max(TEST["rule"]["sma_days"])
    return closes.rolling(longest, min_periods=longest).mean().first_valid_index()


def quality_summary(path: Path) -> str:
    """What the data looks like - gaps, zero volume, spikes, big moves. No rule results."""
    raw = load_raw(path)
    closes, filled = clean(raw)
    start = trading_start(closes)
    period = closes[closes.index >= start] if start is not None else closes.iloc[0:0]
    in_period = filled[filled["day"] >= start] if len(filled) and start is not None else filled.iloc[0:0]
    lines = [f"Pre-2017 BTC data quality - {path} (NO rule results)",
             f"  SHA-256: {sha256(path)}",
             f"  Raw candles: {len(raw)} from {raw.index[0]:%Y-%m-%d} to {raw.index[-1]:%Y-%m-%d}; calendar days "
             f"{len(closes)} ({closes.index[0]:%Y-%m-%d} to {closes.index[-1]:%Y-%m-%d})"]
    if len(filled):
        by = filled.groupby("reason").size()
        lines.append("  Filled days (all): " + ", ".join(f"{n} {r}" for r, n in by.items()))
    else:
        lines.append("  Filled days (all): none")
    missing = filled[filled["reason"] == "no candle"]["day"] if len(filled) else pd.Series(dtype="datetime64[ns, UTC]")
    if len(missing):
        runs, run_start, run_len, last = [], None, 0, None
        for d in missing:
            if last is not None and d - last == DAY:
                run_len += 1
            else:
                if run_start is not None:
                    runs.append((run_len, run_start))
                run_start, run_len = d, 1
            last = d
        runs.append((run_len, run_start))
        runs.sort(key=lambda x: (-x[0], x[1]))
        lines.append("  Longest gaps (no candle): " + "; ".join(f"{n} day(s) from {s:%Y-%m-%d}" for n, s in runs[:10]))
    zv = filled[filled["reason"] == "zero volume"]["day"] if len(filled) else []
    if len(zv):
        lines.append(f"  Zero-volume days: {len(zv)} (first: " + ", ".join(f"{d:%Y-%m-%d}" for d in list(zv)[:8]) + ")")
    bad = filled[filled["reason"].str.startswith("bad print")] if len(filled) else filled
    for _, r in bad.iterrows():
        lines.append(f"  Bad print replaced: {r['day']:%Y-%m-%d} close {r['raw_close']:.2f} (previous {r['used']:.2f}, "
                     f"next {r['next_close']:.2f})")
    moves = np.log(closes / closes.shift(1)).dropna()
    moves = moves[moves != 0]
    top = moves.reindex(moves.abs().sort_values(ascending=False).index)[:10]
    lines.append("  Largest daily close-to-close moves after cleaning: "
                 + ", ".join(f"{d:%Y-%m-%d} {np.expm1(m):+.0%}" for d, m in top.items()))
    wide = raw[(raw["high"] / raw["low"]) > 2]
    if len(wide):
        lines.append(f"  Days with high/low > 2x (intraday; closes only are used): {len(wide)} (e.g. "
                     + ", ".join(f"{d:%Y-%m-%d}" for d in wide.index[:5]) + ")")
    if start is not None:
        share = len(in_period) / max(len(period), 1)
        lines.append(f"  Trading period after the 200-day warm-up: {start:%Y-%m-%d} to {END:%Y-%m-%d}, {len(period)} days; "
                     f"filled {len(in_period)} ({share:.1%}) - {'usable' if share <= 0.05 else 'UNUSABLE'} "
                     f"under the 5% limit")
    return "\n".join(lines)


# ---------------------------------------------------------------- registration and the run
def _folder(state_dir) -> Path:
    f = Path(state_dir) / "research"
    f.mkdir(parents=True, exist_ok=True)
    return f


def registration(state_dir) -> dict | None:
    p = _folder(state_dir) / f"{TEST['id']}.registration.json"
    return json.loads(p.read_text()) if p.exists() else None


def result(state_dir) -> dict | None:
    p = _folder(state_dir) / f"{TEST['id']}.json"
    return json.loads(p.read_text()) if p.exists() else None


def _ledger(state_dir, entry: dict) -> None:
    with open(_folder(state_dir) / "ledger.jsonl", "a") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")


def register(state_dir, data_dir, code: str) -> dict:
    if registration(state_dir):
        raise RuntimeError(f"{TEST['id']} is already registered - a registration is never changed")
    path = data_path(data_dir)
    if not path.exists():
        raise RuntimeError("fetch the data first (tradebot research oos-fetch)")
    reg = {"id": TEST["id"], "registered_at": time.strftime("%Y-%m-%d %H:%M:%S"), "protocol": TEST,
           "data_file": str(path), "data_sha256": sha256(path), "code": code}
    (_folder(state_dir) / f"{TEST['id']}.registration.json").write_text(json.dumps(reg, indent=2, default=str))
    _ledger(state_dir, {"id": TEST["id"], "kind": "registration", "at": reg["registered_at"],
                        "data_sha256": reg["data_sha256"], "code": code, "variants_tested": 1})
    return reg


def run(state_dir, data_dir, code: str) -> dict:
    from .portfolio import curve_stats, worst_dip

    reg = registration(state_dir)
    if not reg:
        raise RuntimeError(f"{TEST['id']} is not registered - register the protocol before running it")
    if result(state_dir):
        raise RuntimeError(f"{TEST['id']} already ran - it runs once (tradebot research oos-show)")
    path = data_path(data_dir)
    if not path.exists() or sha256(path) != reg["data_sha256"]:
        raise RuntimeError("the data file is missing or differs from the registered one (SHA-256 mismatch) - "
                           "the test runs only on the registered data")
    closes, filled = clean(load_raw(path))
    start = trading_start(closes)
    in_period = filled[filled["day"] >= start] if len(filled) else filled
    period_days = int((closes.index >= start).sum())
    share = len(in_period) / max(period_days, 1)
    rule = TEST["rule"]
    out = {"id": TEST["id"], "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"), "code": code,
           "registered_at": reg["registered_at"], "data_sha256": reg["data_sha256"],
           "trading_period": [f"{start:%Y-%m-%d}", f"{END:%Y-%m-%d}"],
           "measured_from": f"{start - DAY:%Y-%m-%d}", "filled_share": share}
    if share > 0.05:
        out.update(verdict="unusable", reading="Data unusable under the registered rule: no verdict.")
    else:
        cfg = replace(CoreConfig(), fraction=1.0, symbols=["BTC/USD"], sma_days=list(rule["sma_days"]),
                      drift_tolerance=rule["drift_tolerance"], min_trade_usd=rule["min_trade_usd"])
        detail = simulate_core_detail({"BTC/USD": closes}, cfg, Costs(FEE, 0.0), start_equity=rule["account_usd"])
        base = start - DAY  # the close of day 199: the full account in cash, before any trade
        core = detail["equity"][detail.index >= base]
        px = closes[closes.index >= start]
        hold = pd.concat([pd.Series([rule["account_usd"]], index=[base]),
                          px / px.iloc[0] * rule["account_usd"] * (1 - FEE)])  # bought at day 200's close
        stats = {}
        for name, curve in (("core", core), ("hold", hold)):
            s = curve_stats(curve)
            dip, top, low = worst_dip(curve)
            stats[name] = {"cagr_pct": s["cagr_pct"], "total_pct": s["total_pct"], "sharpe": s["sharpe"],
                           "worst_dip_pct": dip, "dip_peak": f"{top:%Y-%m-%d}", "dip_low": f"{low:%Y-%m-%d}"}
        part = detail[detail.index >= start]
        stats["core"].update(trades=int(part["trades"].sum()), costs_usd=float(part["costs"].sum()),
                             avg_exposure=float((part["invested"] / part["equity"]).mean()))
        sharpe_ok = stats["core"]["sharpe"] >= stats["hold"]["sharpe"]
        dip_ok = stats["core"]["worst_dip_pct"] < stats["hold"]["worst_dip_pct"]
        verdict = "pass" if sharpe_ok and dip_ok else "dip_only" if dip_ok else "fail"
        out.update(stats=stats, sharpe_ok=sharpe_ok, dip_ok=dip_ok, verdict=verdict,
                   reading=TEST["reading"][verdict])
    (_folder(state_dir) / f"{TEST['id']}.json").write_text(json.dumps(out, indent=2, default=str))
    _ledger(state_dir, {"id": TEST["id"], "kind": "result", "at": out["ran_at"], "verdict": out["verdict"],
                        "code": code, "data_sha256": reg["data_sha256"]})
    return out


def format_protocol() -> str:
    t = TEST
    lines = [f"Protocol {t['id']}: {t['question']}", f"  Data: {t['data']}", "  Cleaning:"]
    lines += [f"    - {c}" for c in t["cleaning"]]
    r = t["rule"]
    lines += [f"  Rule: {r['description']}: averages {'/'.join(map(str, r['sma_days']))} days, {r['steps']}, drift "
              f"tolerance {r['drift_tolerance']:.0%}, minimum trade ${r['min_trade_usd']:g}, test account "
              f"${r['account_usd']:,.0f}.",
              f"  Warm-up: {t['warmup']}", f"  Costs: {t['costs']}", f"  Benchmark: {t['benchmark']}",
              f"  Metrics: {t['metrics']}", f"  Pass: {t['pass']}", "  Reading, fixed now:"]
    lines += [f"    - {v}" for v in t["reading"].values()]
    lines.append(f"  Runs: {t['runs']}")
    return "\n".join(lines)


def format_result(res: dict) -> str:
    lines = [f"Out-of-sample test {res['id']} (registered {res['registered_at']}, ran {res['ran_at']}, code "
             f"{res['code']})",
             f"  Trading period: first possible trade {res['trading_period'][0]}, to {res['trading_period'][1]}; "
             f"returns measured from the close of {res.get('measured_from', '?')}; filled days {res['filled_share']:.1%}"]
    if res.get("stats"):
        s = res["stats"]
        lines.append(f"  {'':<18}{'per year':>10}{'total':>10}{'worst dip':>11}{'dip (peak -> low)':>28}{'Sharpe':>8}")
        for key, name in (("core", "core rule"), ("hold", "buy-and-hold BTC")):
            x = s[key]
            lines.append(f"  {name:<18}{x['cagr_pct']:>+9.1f}%{x['total_pct']:>+9.0f}%{-x['worst_dip_pct']:>10.1f}%"
                         f"{x['dip_peak'] + ' -> ' + x['dip_low']:>28}{x['sharpe']:>8.2f}")
        c = s["core"]
        lines.append(f"  core rule: {c['trades']} trades, costs ${c['costs_usd']:,.2f}, average exposure "
                     f"{c['avg_exposure']:.0%}")
        lines.append(f"  Sharpe at least buy-and-hold's: {'yes' if res['sharpe_ok'] else 'no'}; shallower worst dip: "
                     f"{'yes' if res['dip_ok'] else 'no'}")
    lines.append(f"  VERDICT: {res['verdict'].upper()} - {res['reading']}")
    return "\n".join(lines)
