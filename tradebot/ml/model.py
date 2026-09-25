"""The self-learning trade filter.

A gradient-boosted classifier estimates P(trade is profitable) for each signal.
Training is walk-forward and leak-free:

    |---- train (60%) ----|-- calibrate (20%) --|-- test (20%) --|   (by time)

* rows whose outcome is only known after the next block starts are dropped
  (an "embargo"), so no future information leaks backwards;
* the probability threshold is chosen on the calibration block;
* the decision to deploy is made on the untouched test block: the filtered
  signals must earn clearly more per trade than taking every signal, and must
  not be worse than the currently deployed model on data it has never seen.

Only then is the model refit on all data and saved. Otherwise the old model (or
no model) stays in place - the bot never "learns" its way into something worse
without evidence.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from ..config import MLConfig
from .features import FEATURE_COLUMNS

MODEL_VERSION = 1


def _make_classifier() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_depth=3,
        learning_rate=0.05,
        max_iter=200,
        min_samples_leaf=40,
        l2_regularization=1.0,
        random_state=0,
    )


@dataclass
class ModelReport:
    trained_at: float
    promoted: bool
    reason: str
    n_candidates: int = 0
    n_train: int = 0
    n_calib: int = 0
    n_test: int = 0
    threshold: float = 0.5
    auc_test: float | None = None
    base_expectancy_r: float = 0.0
    base_win_rate: float = 0.0
    filtered_expectancy_r: float = 0.0
    filtered_win_rate: float = 0.0
    filtered_trades: int = 0
    vs_current: dict | None = None
    trained_until: str | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        head = "PROMOTED" if self.promoted else "not deployed"
        if not self.n_test or (self.auc_test is None and not self.filtered_trades):
            return f"ML filter {head}: {self.reason}"
        auc = f"{self.auc_test:.3f}" if self.auc_test is not None else "n/a"
        return (
            f"ML filter {head}: {self.reason}\n"
            f"  test set: all signals {self.base_expectancy_r:+.3f}R ({self.base_win_rate:.0%} win, {self.n_test} trades) "
            f"-> filtered {self.filtered_expectancy_r:+.3f}R ({self.filtered_win_rate:.0%} win, {self.filtered_trades} trades), "
            f"threshold {self.threshold:.2f}, AUC {auc}"
        )


class SignalModel:
    def __init__(self, clf, threshold: float, report: ModelReport, feature_columns=None):
        self.clf = clf
        self.threshold = float(threshold)
        self.report = report
        self.feature_columns = list(feature_columns or FEATURE_COLUMNS)

    @property
    def trained_until(self) -> pd.Timestamp | None:
        return pd.Timestamp(self.report.trained_until) if self.report.trained_until else None

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        if len(X) == 0:
            return np.array([])
        Xa = X.reindex(columns=self.feature_columns).to_numpy(dtype=float)
        return self.clf.predict_proba(Xa)[:, 1]

    def save(self, path: Path) -> None:
        joblib.dump(
            {"version": MODEL_VERSION, "clf": self.clf, "threshold": self.threshold,
             "report": asdict(self.report), "feature_columns": self.feature_columns},
            path,
        )

    @classmethod
    def load(cls, path: Path) -> "SignalModel | None":
        if not Path(path).exists():
            return None
        d = joblib.load(path)
        if d.get("version") != MODEL_VERSION or d.get("feature_columns") != FEATURE_COLUMNS:
            return None  # stale model from an older feature set; retrain
        return cls(d["clf"], d["threshold"], ModelReport(**d["report"]), d["feature_columns"])


def _fit(df: pd.DataFrame):
    clf = _make_classifier()
    clf.fit(df[FEATURE_COLUMNS].to_numpy(dtype=float), df["label"].to_numpy(dtype=int))
    return clf


def _filtered_stats(r: np.ndarray, probs: np.ndarray, thr: float) -> tuple[float, float, int]:
    take = probs >= thr
    n = int(take.sum())
    if n == 0:
        return 0.0, 0.0, 0
    sel = r[take]
    return float(sel.mean()), float((sel > 0).mean()), n


def train_model(
    cands: pd.DataFrame,
    cfg: MLConfig,
    current: SignalModel | None = None,
    focus: set[tuple[str, str]] | None = None,
) -> tuple[SignalModel | None, ModelReport]:
    """Train on every candidate, but tune the threshold and judge the model only on
    the ``focus`` (strategy, timeframe) combos - the ones the bot actually trades."""
    now = time.time()
    n = len(cands)
    if n < cfg.min_candidates:
        return None, ModelReport(now, False, f"not enough historical signals ({n} < {cfg.min_candidates})", n_candidates=n)

    df = cands.sort_values("signal_time", kind="stable").reset_index(drop=True)
    st, et = pd.to_datetime(df["signal_time"], utc=True), pd.to_datetime(df["exit_time"], utc=True)
    t1, t2 = st.iloc[int(n * 0.6)], st.iloc[int(n * 0.8)]
    train = df[(st < t1) & (et < t1)]
    calib = df[(st >= t1) & (st < t2) & (et < t2)]
    test = df[st >= t2]
    if focus:
        def in_focus(frame):
            keys = list(zip(frame["strategy"], frame["timeframe"]))
            return frame[[k in focus for k in keys]]

        calib, test = in_focus(calib), in_focus(test)
    rep = ModelReport(now, False, "", n_candidates=n, n_train=len(train), n_calib=len(calib), n_test=len(test))
    if train["label"].nunique() < 2 or len(calib) < 20 or len(test) < cfg.min_test_trades:
        rep.reason = "not enough data in each walk-forward block"
        return None, rep

    clf = _fit(train)
    p_cal = clf.predict_proba(calib[FEATURE_COLUMNS].to_numpy(dtype=float))[:, 1]
    r_cal = calib["r_multiple"].to_numpy()
    min_take = max(10, int(0.25 * len(calib)))
    best_thr, best_exp = cfg.threshold_grid[0], -math.inf
    for thr in cfg.threshold_grid:
        exp, _, taken = _filtered_stats(r_cal, p_cal, thr)
        if taken >= min_take and exp > best_exp:
            best_thr, best_exp = thr, exp
    thr = cfg.min_probability if cfg.min_probability is not None else best_thr
    rep.threshold = float(thr)

    p_test = clf.predict_proba(test[FEATURE_COLUMNS].to_numpy(dtype=float))[:, 1]
    r_test = test["r_multiple"].to_numpy()
    y_test = test["label"].to_numpy()
    rep.base_expectancy_r = float(r_test.mean())
    rep.base_win_rate = float((r_test > 0).mean())
    rep.filtered_expectancy_r, rep.filtered_win_rate, rep.filtered_trades = _filtered_stats(r_test, p_test, thr)
    if len(np.unique(y_test)) == 2:
        rep.auc_test = float(roc_auc_score(y_test, p_test))

    problems = []
    if rep.filtered_trades < cfg.min_test_trades:
        problems.append(f"filter kept only {rep.filtered_trades} test trades (< {cfg.min_test_trades})")
    if rep.filtered_expectancy_r <= 0:
        problems.append(f"filtered expectancy {rep.filtered_expectancy_r:+.3f}R is not positive")
    if rep.filtered_expectancy_r < rep.base_expectancy_r + cfg.min_improvement_r:
        problems.append("filter does not beat taking every signal on unseen data")

    if current is not None and current.trained_until is not None:
        mask = (pd.to_datetime(test["signal_time"], utc=True) > current.trained_until).to_numpy()
        unseen = test[mask]
        if len(unseen) >= cfg.min_test_trades:
            r_u = unseen["r_multiple"].to_numpy()
            new_exp, _, new_n = _filtered_stats(r_u, p_test[mask], thr)
            cur_exp, _, cur_n = _filtered_stats(r_u, current.predict_proba(unseen), current.threshold)
            rep.vs_current = {"unseen_trades": len(unseen), "new_expectancy_r": new_exp, "new_taken": new_n,
                              "current_expectancy_r": cur_exp, "current_taken": cur_n}
            if cur_n >= 10 and new_exp < cur_exp:
                problems.append(f"worse than current model on unseen data ({new_exp:+.3f}R vs {cur_exp:+.3f}R)")

    if problems:
        rep.reason = "; ".join(problems)
        return None, rep

    final = _fit(df)  # refit on everything, keeping the validated hyper-parameters/threshold
    rep.promoted = True
    rep.reason = "beat the unfiltered strategy on unseen data"
    rep.trained_until = pd.Timestamp(et.max()).isoformat()
    return SignalModel(final, thr, rep), rep


def save_report(rep: ModelReport, path: Path) -> None:
    path.write_text(json.dumps(asdict(rep), indent=2, default=str))
