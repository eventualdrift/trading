"""Gate 1's same-trades check: does a change alter what the bot trades?

The live path (scanner, risk checks, entries, exits, core rebalance) is replayed through the real
``TradingBot`` on a fixed dataset stored in the repository, and every order it makes is compared
with a stored golden file. Identical orders: the change doesn't affect trades. Different orders:
the golden file may only be rewritten for a change that passed a registered test (``--accept``),
and the deploy gate checks that.

The dataset is synthetic and stored, not regenerated, so a change to the synthetic generator
can't move it. The replay's config is the code's defaults (plus the fixed overrides below), so a
changed default is caught too. The ML filter is off, as in paper (CLAUDE.md).
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


class ReplayMarket(SyntheticMarket):
    """The synthetic market's interface, serving the stored dataset."""

    def __init__(self, data_dir: Path = DATA_DIR):  # noqa: D107 - deliberately not calling super().__init__
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


def _r(x):
    return None if x is None else float(f"{float(x):.10g}")


def replay(days: int = REPLAY_DAYS, overrides: dict | None = None, data_dir: Path = DATA_DIR) -> dict:
    """Run the live bot on the fixed dataset and return every order and decision it made."""
    quiet = logging.getLogger("tradebot")
    level = quiet.level
    quiet.setLevel(logging.WARNING)  # the replay's scans would print thousands of lines
    try:
        return _replay(days, overrides, data_dir)
    finally:
        quiet.setLevel(level)


def _replay(days: int, overrides: dict | None, data_dir: Path) -> dict:
    from ..backtest.selection import ComboResult, Selection
    from ..bot import TradingBot
    from ..db import Database
    from ..execution import PaperBroker
    from ..models import Position, Signal
    from ..notify import MemoryNotifier

    with tempfile.TemporaryDirectory() as tmp:
        cfg = replay_config(Path(tmp), overrides)
        market = ReplayMarket(data_dir)
        start = market.start_ms + REPLAY_FROM_DAY * DAY
        market.set_now(start)
        db = Database(Path(tmp) / "tradebot.db")
        selection = Selection(0.0, [ComboResult(s, tf, {}, {}, {}, 1, 1.0, True) for s, tf in SELECTION])
        bot = TradingBot(cfg, market, PaperBroker(db, cfg.costs_model(), cfg.paper.starting_balance, market),
                         db, MemoryNotifier(), selection=selection)
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


def write_golden(orders: dict, accepted_by: str | None, code: str, path: Path | None = None,
                 days: int = REPLAY_DAYS) -> None:
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
    kinds = ("positions", "signals", "core_trades")
    for n, kind in enumerate(kinds):
        rows = orders.get(kind, [])
        lines.append(f"  {json.dumps(kind)}: [")  # one order per line: readable diffs
        lines += [f"   {json.dumps(r)}{',' if i < len(rows) - 1 else ''}" for i, r in enumerate(rows)]
        lines.append("  ]" + ("," if n < len(kinds) - 1 else ""))
    path.write_text("\n".join(lines + [" }", "}"]) + "\n")


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
