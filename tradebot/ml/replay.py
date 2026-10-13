"""The ML trade filter in the account replay, walk-forward: the model a weekly learn would have
had at each signal, trained only on signals whose outcome was known by that learn.

Mirrors the live cycle (``learning.learning_cycle`` and ``scanner.Scanner``): every week a learn
trains on the candidates closed by then and promotes the model only if it passes the gate in
``train_model`` (otherwise the model in force stays, or there is none); each later signal is
scored by the model in force at its candle close, filtered below the threshold, sized by the
confidence multiplier and ranked by expected R, as the live scan does. What a backtest can't
have: the bot's own closed trades, which live adds to the training set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..backtest.engine import Trade
from ..config import BotConfig
from ..timeframes import tf_ms
from .features import FEATURE_COLUMNS
from .model import SignalModel, train_model

WEEK_MS = 168 * 3_600_000


def _ms(values) -> "pd.Index":
    """Timestamps -> UTC milliseconds (naive ones are taken as UTC)."""
    idx = pd.DatetimeIndex(pd.to_datetime(values, utc=True))
    return idx.as_unit("ns").asi8 // 1_000_000


@dataclass
class Learn:
    at_ms: int
    promoted: bool
    reason: str
    model: SignalModel | None  # the model in force after this learn: promoted here, or kept
    trained_until: str | None = None
    n_candidates: int = 0  # signals whose trade had closed by this learn


@dataclass
class MLReplay:
    learns: list[Learn] = field(default_factory=list)
    scored: int = 0
    filtered: int = 0
    sized_up: int = 0
    unscored: int = 0  # signals a model was in force for but with no features (trade incomplete at the data end)
    multipliers: list[float] = field(default_factory=list)

    @property
    def promotions(self) -> list[Learn]:
        return [x for x in self.learns if x.promoted]

    def model_at(self, at_ms: int) -> SignalModel | None:
        """The model in force at ``at_ms``: the one left by the last learn at or before it."""
        model = None
        for x in self.learns:
            if x.at_ms > at_ms:
                break
            model = x.model
        return model

    def in_force_from(self) -> int | None:
        """When a model was first in force (the first promotion), or None."""
        p = self.promotions
        return p[0].at_ms if p else None


def learn_times(start_ms: int, end_ms: int, every_ms: int = WEEK_MS) -> list[int]:
    """The weekly learns a bot started at ``start_ms`` would have run: the first one a week in."""
    return list(range(int(start_ms) + every_ms, int(end_ms) + 1, every_ms))


def walk_forward(cands: pd.DataFrame | None, cfg: BotConfig, times: list[int], focus: set | None,
                 train=train_model) -> MLReplay:
    """One learn per time in ``times``: trained on the candidates whose trade had closed by then
    (never a label from after it), through the live gate; the current model is kept unless the
    new one is promoted."""
    out = MLReplay()
    if cands is None or cands.empty:
        out.learns = [Learn(t, False, "no historical signals", None) for t in times]
        return out
    exit_ms = _ms(cands["exit_time"])
    current = None
    for t in times:
        known = cands[exit_ms < t]
        model, rep = train(known, cfg.ml, current, focus=focus or None)
        if model is not None:
            current = model
        out.learns.append(Learn(int(t), bool(rep.promoted), rep.reason, current, rep.trained_until, len(known)))
    return out


def risk_multiplier(prob: float, threshold: float, top: float, scaling: bool) -> float:
    """``Scanner.risk_multiplier``: up to ``top`` x risk on the strongest setups, only for a model
    whose confident picks proved to earn more."""
    if not scaling or top <= 1.0:
        return 1.0
    frac = min(max((prob - threshold) / max(1.0 - threshold, 1e-9), 0.0), 1.0)
    return 1.0 + frac * (top - 1.0)


def apply(trades: list[Trade], cands: pd.DataFrame | None, replay: MLReplay, cfg: BotConfig) -> MLReplay:
    """Score every trade with the model in force at its candle close and record on it (``ml``)
    what the live scan would have done: the probability and threshold, filtered or not, the risk
    multiplier and the expected R it is ranked by. No model in force: ``ml`` stays None (taken at
    1x, ranked by reward:risk, as live before its first promoted model)."""
    top = cfg.risk.max_risk_multiplier
    replay.scored = replay.filtered = replay.sized_up = replay.unscored = 0  # this application's counts
    replay.multipliers = []
    rows: dict[tuple, int] = {}
    if cands is not None and not cands.empty:
        for i, key in enumerate(zip(cands["symbol"], cands["timeframe"], cands["strategy"], _ms(cands["signal_time"]))):
            rows.setdefault(key, i)
    by_model: dict[int, tuple[SignalModel, list[tuple[Trade, int]]]] = {}
    for t in trades:
        t.ml = None
        signal_ms = int(_ms([t.signal_time])[0])
        model = replay.model_at(signal_ms + tf_ms(t.timeframe))  # the scan runs after the candle closes
        if model is None:
            continue
        row = rows.get((t.symbol, t.timeframe, t.strategy, signal_ms))
        if row is None:
            replay.unscored += 1
            continue
        by_model.setdefault(id(model), (model, []))[1].append((t, row))
    for model, items in by_model.values():
        probs = model.predict_proba(cands.iloc[[r for _, r in items]][FEATURE_COLUMNS])
        thr = float(cfg.ml.min_probability if cfg.ml.min_probability is not None else model.threshold)
        scaling = bool(getattr(getattr(model, "report", None), "confidence_scaling", False))
        for (t, _), p in zip(items, probs):
            p = float(p)
            mult = risk_multiplier(p, thr, top, scaling)
            t.ml = {"probability": p, "threshold": thr, "filtered": p < thr, "multiplier": mult,
                    "expected_r": p * min(t.signal_rr, 3.0) - (1 - p)}
            replay.scored += 1
            replay.filtered += int(p < thr)
            if mult > 1.0 + 1e-9:
                replay.sized_up += 1
                replay.multipliers.append(mult)
    return replay


def describe(replay: MLReplay | None, skipped_r: list[float] | None = None) -> list[str]:
    """The report lines: the learns and promotions, what the filter did."""
    if replay is None:
        return ["  ML filter: not replayed (off in code: config.ML_IN_REPLAY; --ml-replay models it walk-forward)"]
    promos = replay.promotions
    when = ", ".join(pd.Timestamp(x.at_ms, unit="ms", tz="UTC").strftime("%Y-%m-%d") for x in promos[:8])
    head = (f"  ML filter (walk-forward: the model a weekly learn would have had, trained on signals closed by "
            f"then): {len(replay.learns)} learns, {len(promos)} promoted"
            + (f" ({when}{', ...' if len(promos) > 8 else ''})" if promos else " - no model was ever in force"))
    if not promos:
        return [head]
    import numpy as np

    r = [x for x in (skipped_r or []) if x is not None]
    filt = (f"filtered {replay.filtered}" + (f" (as trades: {sum(v > 0 for v in r)} won of {len(r)}, total "
                                           f"{sum(r):+.1f}R)" if r else ""))
    sized = (f"sized up {replay.sized_up} (x{np.mean(replay.multipliers):.2f} on average, max "
             f"x{max(replay.multipliers):.2f})" if replay.multipliers else "sized up none")
    return [head, f"    scored {replay.scored} signals: {filt}; {sized}"
                  + (f"; {replay.unscored} not scored (no features: trade incomplete at the data end)"
                     if replay.unscored else "")]
