"""Gate 1's same-trades check: does a change alter what the bot trades?

The live path (scanner, risk checks, entries, exits, core rebalance) is replayed through the real
``TradingBot`` on a fixed dataset stored in the repository, and every order it makes is compared
with a stored golden file. Identical orders: the change doesn't affect trades. Different orders:
the golden file may only be rewritten for a change that passed a registered test (``--accept``),
and the deploy gate checks that.

The dataset is synthetic and stored, not regenerated, so a change to the synthetic generator
can't move it. The replay's config is the code's defaults (plus the fixed overrides below), so a
changed default is caught too. The ML filter is off, as in paper (CLAUDE.md).

Four stored scenarios run after the plain replay, so that what the golden orders can't see is
seen too: a feed that publishes candles late (``stale-candles``), fetches that fail and recover
(``fetch-failures``), a stored model that filters and sizes signals (``ml-model``) and the code's
default risk limits divided by stored factors, so that the daily loss limit and the drawdown halt
fire on the dataset (``breakers``; the defaults themselves never do in these 40 days): a change to
how either breaker acts, or to a default limit, moves its orders. Their schedules, factors and the
model live in ``data/`` and are never regenerated; the golden file records each scenario's orders
in its own section.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import tempfile
from pathlib import Path

import pandas as pd

from ..data.synthetic import SyntheticMarket
from ..timeframes import tf_ms

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
GOLDEN = HERE / "golden.json"
DAY = 86_400_000
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
SELECTION = [("momentum", "4h"), ("breakout", "1d"), ("breakout", "1h")]
DATASET_DAYS, DATASET_SEED = 300, 7
REPLAY_FROM_DAY, REPLAY_DAYS = 240, 40  # the core's 200-day averages exist from day 240
SCENARIOS = ("stale-candles", "fetch-failures", "ml-model", "breakers")


# --------------------------------------------------------------------- the dataset
def _file(symbol: str) -> str:
    return symbol.replace("/", "-") + ".csv.gz"


def make_dataset(out_dir: Path = DATA_DIR) -> None:
    """Write the fixed dataset (run once; the files are committed and never regenerated)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    m = SyntheticMarket(SYMBOLS, days=DATASET_DAYS, base_tf="1h", seed=DATASET_SEED)
    for s in SYMBOLS:
        df = m.base[s].copy()
        df.insert(0, "time", df.index.as_unit("ms").asi8)
        buf = io.StringIO()
        df.to_csv(buf, index=False, float_format="%.10g")
        with gzip.GzipFile(out_dir / _file(s), "wb", mtime=0) as fh:  # mtime 0: the same bytes every time
            fh.write(buf.getvalue().encode())


def dataset_sha256(data_dir: Path = DATA_DIR) -> str:
    h = hashlib.sha256()
    for s in SYMBOLS:
        h.update(gzip.decompress((data_dir / _file(s)).read_bytes()))
    return h.hexdigest()


# --------------------------------------------------------------------- the scenarios
def scenario_path(name: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / f"scenario-{name}.json"


def load_scenario(name: str, data_dir: Path = DATA_DIR) -> dict:
    if name not in SCENARIOS:
        raise ValueError(f"unknown scenario {name!r}: one of {', '.join(SCENARIOS)}")
    return json.loads(scenario_path(name, data_dir).read_text())


def scenario_sha256(name: str, data_dir: Path = DATA_DIR) -> str:
    return hashlib.sha256(scenario_path(name, data_dir).read_bytes()).hexdigest()


def _iso_ms(text: str) -> int:
    return int(pd.Timestamp(text).tz_convert("UTC").value // 1_000_000) if pd.Timestamp(text).tzinfo \
        else int(pd.Timestamp(text, tz="UTC").value // 1_000_000)


class StoredModel:
    """A stored stand-in for the ML filter with SignalModel's interface (predict_proba, threshold,
    report): a logistic score over standardised feature columns, kept as JSON so that it loads the
    same on any library version. Made once by ``make_scenario_model``; never regenerated."""

    def __init__(self, spec: dict):
        from ..ml.features import FEATURE_COLUMNS
        from ..ml.model import ModelReport

        self.spec = spec
        self.feature_columns = list(FEATURE_COLUMNS)
        self.threshold = float(spec["threshold"])
        self.report = ModelReport(0.0, True, "stored scenario model", threshold=self.threshold,
                                  confidence_scaling=bool(spec.get("confidence_scaling", True)),
                                  trained_until=spec.get("trained_until"))

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> "StoredModel":
        return cls(load_scenario("ml-model", data_dir))

    def predict_proba(self, X: pd.DataFrame):
        import numpy as np

        z = np.full(len(X), float(self.spec["bias"]))
        for col, w in self.spec["weights"].items():
            v = X[col].to_numpy(dtype=float) if col in X.columns else np.full(len(X), np.nan)
            v = (v - self.spec["mean"][col]) / (self.spec["std"][col] or 1.0)
            z += w * np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)
        return 1.0 / (1.0 + np.exp(-z))


def make_scenario_model(data_dir: Path = DATA_DIR, threshold_quantile: float = 0.4) -> dict:
    """Fit the stored model once, on the dataset before the replay starts (the signals of the
    replayed strategies, labelled by their own simulated outcome, as learn does), and write it."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    from ..config import BotConfig
    from ..ml.dataset import build_candidates
    from ..ml.features import FEATURE_COLUMNS

    market = ReplayMarket(data_dir)
    market.set_now(market.start_ms + REPLAY_FROM_DAY * DAY)  # nothing the replay will see
    cfg = BotConfig()
    tfs = sorted({tf for _, tf in SELECTION}, key=tf_ms)
    datasets = {tf: {s: market.fetch_ohlcv_df(s, tf, limit=10**6) for s in SYMBOLS} for tf in tfs}
    cfg.strategies = {name: {} for name, _ in SELECTION}
    cands = build_candidates(datasets, cfg, None)
    X = cands[FEATURE_COLUMNS].to_numpy(dtype=float)
    mean, std = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
    mean, std = np.nan_to_num(mean), np.where(np.nan_to_num(std) > 0, np.nan_to_num(std), 1.0)
    Z = np.nan_to_num((X - mean) / std)
    clf = LogisticRegression(C=0.5, max_iter=1000).fit(Z, cands["label"].to_numpy())
    probs = clf.predict_proba(Z)[:, 1]
    spec = {"name": "ml-model", "kind": "logistic", "trained_on": f"{len(cands)} signals of the dataset before "
            f"replay day {REPLAY_FROM_DAY}", "trained_until": str(pd.Timestamp(cands["exit_time"].max())),
            "threshold": float(f"{np.quantile(probs, threshold_quantile):.4f}"), "confidence_scaling": True,
            "bias": float(f"{clf.intercept_[0]:.6g}"),
            "weights": {c: float(f"{w:.6g}") for c, w in zip(FEATURE_COLUMNS, clf.coef_[0])},
            "mean": {c: float(f"{m:.6g}") for c, m in zip(FEATURE_COLUMNS, mean)},
            "std": {c: float(f"{v:.6g}") for c, v in zip(FEATURE_COLUMNS, std)}}
    scenario_path("ml-model", data_dir).write_text(json.dumps(spec, indent=1) + "\n")
    return spec


class ReplayMarket(SyntheticMarket):
    """The synthetic market's interface, serving the stored dataset - and, for a scenario, a feed
    that publishes some candles late (``late``: base candles opening in [from, to) appear only
    ``late_minutes`` after they close; until then nothing after them is published either) or
    fetches that fail (``outages``: the named methods raise for that symbol, or "*", between from
    and to)."""

    def __init__(self, data_dir: Path = DATA_DIR, scenario: dict | None = None):  # noqa: D107 - no super().__init__
        self.symbols = list(SYMBOLS)
        self.base_tf = "1h"
        self.price_bar_ms = tf_ms("1h")
        self.base = {}
        for s in SYMBOLS:
            df = pd.read_csv(data_dir / _file(s))
            idx = pd.DatetimeIndex(pd.to_datetime(df.pop("time"), unit="ms", utc=True), name="time").as_unit("ns")
            self.base[s] = df.set_index(idx).astype(float)
        self._frames = {}
        self._now = int(self.base[SYMBOLS[0]].index[-1].value // 1_000_000) + self.price_bar_ms
        self.late = [(x["symbol"], _iso_ms(x["from"]), _iso_ms(x["to"]), int(x["late_minutes"]) * 60_000)
                     for x in (scenario or {}).get("late", [])]
        self.outages = [(x["symbol"], set(x["methods"]), _iso_ms(x["from"]), _iso_ms(x["to"]))
                        for x in (scenario or {}).get("outages", [])]

    def _available_until(self, symbol: str) -> int:
        """The feed's clock for ``symbol``: now, or the open of the first candle not yet published."""
        until = self._now
        for sym, start, end, late in self.late:
            if sym != symbol:
                continue
            bar = self.price_bar_ms
            first_open = max(start, int(self.base[symbol].index[0].value // 1_000_000))
            o = first_open - (first_open % bar) + (bar if first_open % bar else 0)
            while o < end:
                if o + bar + late > self._now:  # not published yet: nothing from here on is
                    until = min(until, o)
                    break
                o += bar
        return until

    def _closed(self, symbol: str, tf: str) -> pd.DataFrame:
        import numpy as np

        from ..timeframes import index_ms

        f = self._frame(symbol, tf)
        n = int(np.searchsorted(index_ms(f.index) + tf_ms(tf), self._available_until(symbol), side="right"))
        return f.iloc[:n]

    def _check(self, symbol: str, method: str) -> None:
        for sym, methods, start, end in self.outages:
            if sym in (symbol, "*") and ("*" in methods or method in methods) and start <= self._now < end:
                raise RuntimeError(f"simulated feed outage: {method} {symbol}")

    def fetch_ohlcv_df(self, symbol, tf, limit=300, since=None):
        self._check(symbol, "fetch_ohlcv_df")
        return super().fetch_ohlcv_df(symbol, tf, limit, since)

    def history(self, symbol, tf, start_ms, end_ms=None):
        self._check(symbol, "history")
        return super().history(symbol, tf, start_ms, end_ms)

    def fetch_price_bars(self, symbol, since_ms):
        self._check(symbol, "fetch_price_bars")
        return super().fetch_price_bars(symbol, since_ms)

    def fetch_last_price(self, symbol):
        self._check(symbol, "fetch_last_price")
        return super().fetch_last_price(symbol)

    def fetch_last_price_ts(self, symbol):
        self._check(symbol, "fetch_last_price_ts")
        return super().fetch_last_price_ts(symbol)


# --------------------------------------------------------------------- the replay
def replay_config(state_dir: Path, overrides: dict | None = None):
    from ..config import BotConfig

    cfg = BotConfig(state_dir=str(state_dir))
    cfg.data.dir = str(Path(state_dir) / "data")
    cfg.core.fraction = 0.65  # the account paper runs: 65% core, 35% satellite
    cfg.ml.enabled = False  # paper runs only what the backtest models (CLAUDE.md)
    cfg.dashboard.enabled = False
    cfg.telegram.commands = False
    for key, value in (overrides or {}).items():  # tests: prove a trade-affecting change is caught
        obj = cfg
        *path, last = key.split(".")
        for part in path:
            obj = getattr(obj, part)
        setattr(obj, last, value)
    return cfg


def _tightened(spec: dict | None) -> dict:
    """The breakers scenario's limits: each named default of the code divided by its stored factor
    (the code's defaults, so a changed default changes the scenario's orders too)."""
    from ..config import BotConfig

    out = {}
    for key, factor in ((spec or {}).get("divide_defaults") or {}).items():
        obj = BotConfig()
        for part in key.split("."):
            obj = getattr(obj, part)
        out[key] = float(obj) / float(factor)
    return out


def _r(x):
    return None if x is None else float(f"{float(x):.10g}")


def replay(days: int = REPLAY_DAYS, overrides: dict | None = None, data_dir: Path = DATA_DIR,
           scenario: str | None = None) -> dict:
    """Run the live bot on the fixed dataset and return every order and decision it made; with a
    ``scenario`` (SCENARIOS), under that scenario's feed, model or risk limits."""
    from .. import config as config_module

    quiet = logging.getLogger("tradebot")
    level = quiet.level
    quiet.setLevel(logging.CRITICAL)  # the replay's scans, and a scenario's expected outage errors, stay quiet
    was = config_module.ML_IN_REPLAY
    try:
        if scenario == "ml-model":  # the scenario runs the model path although the bot has it off in code
            config_module.ML_IN_REPLAY = True
        return _replay(days, overrides, data_dir, scenario)
    finally:
        config_module.ML_IN_REPLAY = was
        quiet.setLevel(level)


def _replay(days: int, overrides: dict | None, data_dir: Path, scenario: str | None = None) -> dict:
    from ..backtest.selection import ComboResult, Selection
    from ..bot import TradingBot
    from ..db import Database
    from ..execution import PaperBroker
    from ..models import Position, Signal
    from ..notify import MemoryNotifier

    spec = load_scenario(scenario, data_dir) if scenario else None
    with tempfile.TemporaryDirectory() as tmp:
        cfg = replay_config(Path(tmp), {**_tightened(spec), **(overrides or {})})  # then the caller's (tests)
        model = None
        if scenario == "ml-model":
            cfg.ml.enabled = True
            model = StoredModel(spec)
        market = ReplayMarket(data_dir, spec if scenario in ("stale-candles", "fetch-failures") else None)
        start = market.start_ms + REPLAY_FROM_DAY * DAY
        market.set_now(start)
        db = Database(Path(tmp) / "tradebot.db")
        selection = Selection(0.0, [ComboResult(s, tf, {}, {}, {}, 1, 1.0, True) for s, tf in SELECTION])
        bot = TradingBot(cfg, market, PaperBroker(db, cfg.costs_model(), cfg.paper.starting_balance, market),
                         db, MemoryNotifier(), selection=selection, model=model)
        bar = market.price_bar_ms
        t = start + bar
        while t < start + days * DAY:
            market.set_now(t + 30_000)
            bot.tick(t + 30_000)
            t += bar
        positions = db._select(Position, "SELECT * FROM positions ORDER BY id")
        signals = db._select(Signal, "SELECT * FROM signals ORDER BY id")
        core = sorted(db.core_trades(cfg.mode, 10**6), key=lambda x: (x["ts"], x["symbol"], x["side"]))
        db.close()
    return {
        "positions": [[p.symbol, p.timeframe, p.strategy, p.side, p.status, p.opened_at, _r(p.amount),
                       _r(p.entry_price), _r(p.initial_stop), _r(p.take_profit), p.closed_at, _r(p.exit_price),
                       p.exit_reason, _r(p.fees)] for p in positions],
        "signals": [[s.symbol, s.timeframe, s.strategy, s.side, s.candle_time, s.status, s.note] for s in signals],
        "core_trades": [[c["ts"], c["symbol"], c["side"], _r(c["qty"]), _r(c["price"]), _r(c["fee"]),
                         _r(c["weight_from"]), _r(c["weight_to"]), c["reason"]] for c in core],
    }


# --------------------------------------------------------------------- golden file
def load_golden(path: Path | None = None) -> dict | None:
    path = path or GOLDEN
    return json.loads(path.read_text()) if path.exists() else None


def _order_lines(orders: dict, indent: str) -> list[str]:
    kinds = ("positions", "signals", "core_trades")
    lines = []
    for n, kind in enumerate(kinds):
        rows = orders.get(kind, [])
        lines.append(f"{indent}{json.dumps(kind)}: [")  # one order per line: readable diffs
        lines += [f"{indent} {json.dumps(r)}{',' if i < len(rows) - 1 else ''}" for i, r in enumerate(rows)]
        lines.append(f"{indent}]" + ("," if n < len(kinds) - 1 else ""))
    return lines


def write_golden(orders: dict, accepted_by: str | None, code: str, path: Path | None = None,
                 days: int = REPLAY_DAYS, scenarios: dict | None = None) -> None:
    """Write the golden file: the plain orders, then each recorded scenario's orders in its own
    section (``scenarios``: name -> {"sha256", "recorded_by_code", "orders"})."""
    path = path or GOLDEN
    head = {
        "format": 1,
        "dataset_sha256": dataset_sha256(),
        "replay": {"symbols": SYMBOLS, "selection": [f"{s}@{tf}" for s, tf in SELECTION],
                   "from_day": REPLAY_FROM_DAY, "days": days},
        # a protocol id that passed, when the orders changed on purpose; null for the first file
        "accepted_by": accepted_by,
        "generated_by_code": code,
    }
    lines = ["{"] + [f" {json.dumps(k)}: {json.dumps(v)}," for k, v in head.items()] + [' "orders": {']
    lines += _order_lines(orders, "  ")
    lines.append(" }" + ("," if scenarios else ""))
    if scenarios:
        lines.append(' "scenarios": {')
        names = [n for n in SCENARIOS if n in scenarios]
        for k, name in enumerate(names):
            sc = scenarios[name]
            lines.append(f"  {json.dumps(name)}: {{")
            lines.append(f"   \"sha256\": {json.dumps(sc['sha256'])},")
            lines.append(f"   \"recorded_by_code\": {json.dumps(sc.get('recorded_by_code'))},")
            lines.append('   "orders": {')
            lines += _order_lines(sc["orders"], "    ")
            lines.append("   }")
            lines.append("  }" + ("," if k < len(names) - 1 else ""))
        lines.append(" }")
    path.write_text("\n".join(lines + ["}"]) + "\n")


def differences(expected: dict, got: dict, limit: int = 10) -> list[str]:
    out = []
    for kind in ("positions", "signals", "core_trades"):
        a, b = expected.get(kind, []), got.get(kind, [])
        if a == b:
            continue
        out.append(f"{kind}: {len(a)} in the golden file, {len(b)} now")
        shown = 0
        for i in range(max(len(a), len(b))):
            x, y = (a[i] if i < len(a) else None), (b[i] if i < len(b) else None)
            if x != y and shown < limit:
                out.append(f"  #{i}: golden {x}\n        now    {y}")
                shown += 1
    return out


def summary(orders: dict) -> str:
    closed = sum(p[4] == "closed" for p in orders["positions"])
    return (f"{len(orders['positions'])} positions ({closed} closed), {len(orders['signals'])} signals, "
            f"{len(orders['core_trades'])} core trades")
