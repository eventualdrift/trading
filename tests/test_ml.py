import numpy as np
import pandas as pd
import pytest

from tradebot.config import MLConfig
from tradebot.data.synthetic import generate_ohlcv
from tradebot.ml.dataset import build_candidates
from tradebot.ml.features import DIRECTIONAL, FEATURE_COLUMNS, candidate_features, market_features
from tradebot.ml.model import SignalModel, train_model


def test_market_features_causal(ohlcv_1h):
    full = market_features(ohlcv_1h)
    part = market_features(ohlcv_1h.iloc[:1500])
    a, b = full.iloc[1499], part.iloc[-1]
    assert np.allclose(a.to_numpy(float), b.to_numpy(float), equal_nan=True)


def test_candidate_features_align_direction(ohlcv_1h):
    mkt = market_features(ohlcv_1h).iloc[[500, 500]]
    X = candidate_features(mkt, np.array(["long", "short"]), np.array(["trend", "trend"]), "1h",
                           np.array([100.0, 100.0]), np.array([95.0, 105.0]), np.array([110.0, 90.0]))
    assert list(X.columns) == FEATURE_COLUMNS
    for col in DIRECTIONAL:
        a, b = X[col].iloc[0], X[col].iloc[1]
        assert (np.isnan(a) and np.isnan(b)) or a == pytest.approx(-b)
    assert X["reward_risk"].tolist() == pytest.approx([2.0, 2.0])
    assert X["strat_trend"].tolist() == [1.0, 1.0] and X["side"].tolist() == [1.0, -1.0]


def _fake_candidates(n, informative, seed=0):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    if informative:
        win = rng.random(n) < np.where(X["adx"] > 0, 0.7, 0.2)
    else:
        win = rng.random(n) < 0.4
    X["r_multiple"] = np.where(win, 2.0, -1.0)
    X["label"] = win.astype(int)
    t = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    X["signal_time"], X["exit_time"] = t, t + pd.Timedelta(hours=5)
    X["symbol"], X["timeframe"], X["strategy"] = "X/USDT", "1h", "trend"
    return X


def test_model_promoted_when_it_has_edge(tmp_path):
    cands = _fake_candidates(3000, informative=True)
    model, rep = train_model(cands, MLConfig())
    assert rep.promoted and model is not None, rep.reason
    assert rep.filtered_expectancy_r > rep.base_expectancy_r + 0.1
    assert rep.auc_test > 0.6
    model.save(tmp_path / "m.joblib")
    loaded = SignalModel.load(tmp_path / "m.joblib")
    assert loaded.threshold == model.threshold
    assert np.allclose(loaded.predict_proba(cands.iloc[:10]), model.predict_proba(cands.iloc[:10]))


def test_model_rejected_on_noise():
    model, rep = train_model(_fake_candidates(3000, informative=False, seed=3), MLConfig())
    assert model is None and not rep.promoted


def test_model_needs_enough_data():
    model, rep = train_model(_fake_candidates(100, informative=True), MLConfig(min_candidates=400))
    assert model is None and "not enough" in rep.reason


def test_new_model_must_not_be_worse_than_current():
    good = _fake_candidates(3000, informative=True)
    current, _ = train_model(good.iloc[:2000], MLConfig())
    assert current is not None
    noise = _fake_candidates(3000, informative=False, seed=5)
    noise["signal_time"] = noise["signal_time"] + pd.Timedelta(days=200)
    noise["exit_time"] = noise["exit_time"] + pd.Timedelta(days=200)
    model, rep = train_model(noise, MLConfig(), current=current)
    assert model is None


def test_current_model_kept_until_it_can_be_compared():
    """Review #12: no replacement without enough genuinely unseen signals."""
    good = _fake_candidates(3000, informative=True)
    current, _ = train_model(good, MLConfig())
    assert current is not None
    model, rep = train_model(good, MLConfig(), current=current)  # nothing new since it was trained
    assert model is None and "keeping the current model" in rep.reason


def test_rarely_selecting_incumbent_is_not_replaced_without_evidence():
    """Review: an incumbent that picked only a few unseen trades can't be out-voted by them."""
    good = _fake_candidates(3000, informative=True)
    current, _ = train_model(good.iloc[:2000], MLConfig())
    current.threshold = 0.999  # selects (almost) nothing on new data
    later = good.copy()
    later["signal_time"] = later["signal_time"] + pd.Timedelta(days=200)
    later["exit_time"] = later["exit_time"] + pd.Timedelta(days=200)
    model, rep = train_model(later, MLConfig(), current=current)
    assert model is None and "keeping the current model" in rep.reason


def test_build_candidates_on_prices(cfg):
    df = generate_ohlcv(20000, "15m", seed=4)
    cands = build_candidates({"15m": {"A/USDT": df}}, cfg)
    assert len(cands) > 20
    assert set(cands["label"]) <= {0, 1}
    assert (cands["label"] == (cands["r_multiple"] > 0)).all()
    assert cands["signal_time"].is_monotonic_increasing
    assert (cands["exit_time"] > cands["signal_time"]).all()
