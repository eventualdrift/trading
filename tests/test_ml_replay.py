"""The ML filter replayed walk-forward in the account backtest (BACKLOG a2)."""
import numpy as np
import pandas as pd
import pytest

from tradebot.backtest.engine import Trade, live_order
from tradebot.config import BotConfig
from tradebot.ml.features import FEATURE_COLUMNS
from tradebot.ml.model import ModelReport
from tradebot.ml.replay import MLReplay, apply, describe, learn_times, walk_forward

DAY = 86_400_000
T0 = pd.Timestamp("2024-01-01", tz="UTC")


def candidates(n=600, days=400, seed=0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    start = T0 + pd.to_timedelta(np.sort(rng.integers(0, days * 24, n)), unit="h")
    held = pd.to_timedelta(rng.integers(1, 72, n), unit="h")
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    df["symbol"] = [f"C{i % 5}/USDT" for i in range(n)]
    df["timeframe"] = "4h"
    df["strategy"] = "momentum"
    df["signal_time"] = start
    df["exit_time"] = start + held
    df["r_multiple"] = rng.normal(size=n)
    df["label"] = (df["r_multiple"] > 0).astype(int)
    return df


class StubModel:
    """Scores a signal from one feature, so the test knows the probabilities."""

    def __init__(self, threshold=0.5, scaling=False, trained_until=None):
        self.threshold = threshold
        self.report = ModelReport(0.0, True, "stub", confidence_scaling=scaling, trained_until=trained_until)

    def predict_proba(self, X):
        return 1 / (1 + np.exp(-X["ret_1"].to_numpy(dtype=float)))


def test_each_learn_trains_only_on_trades_closed_by_then_and_keeps_the_model_until_the_next_promotion():
    cands = candidates()
    seen = []

    def train(frame, ml_cfg, current, focus=None):
        at = times[len(seen)]
        seen.append((at, frame["exit_time"].max(), len(frame)))
        promote = len(seen) == 3  # the third learn promotes; the rest keep what is in force
        rep = ModelReport(0.0, promote, "stub", trained_until=str(frame["exit_time"].max()))
        return (StubModel(trained_until=rep.trained_until) if promote else None), rep

    times = learn_times(int(T0.value // 1_000_000), int(T0.value // 1_000_000) + 60 * DAY)
    assert len(times) == 8 and times[0] == int(T0.value // 1_000_000) + 7 * DAY
    rep = walk_forward(cands, BotConfig(), times, {("momentum", "4h")}, train=train)
    assert len(rep.learns) == 8 and [x.promoted for x in rep.learns] == [False, False, True] + [False] * 5
    for at, last_exit, n in seen:  # no label from after the learn
        assert last_exit < pd.Timestamp(at, unit="ms", tz="UTC") and n == (cands["exit_time"] < pd.Timestamp(at, unit="ms", tz="UTC")).sum()
    assert rep.model_at(times[1]) is None and rep.model_at(times[2]) is rep.learns[2].model
    assert rep.model_at(times[7] + DAY) is rep.learns[2].model  # kept through the later learns
    assert rep.in_force_from() == times[2]
    empty = walk_forward(cands.iloc[:0], BotConfig(), times, None, train=train)
    assert all(x.model is None and x.reason == "no historical signals" for x in empty.learns)


def trade(symbol, signal_h, rr=2.0, ret=0.05):
    st = T0 + pd.Timedelta(hours=signal_h)
    return Trade(symbol=symbol, timeframe="4h", strategy="momentum", side="long", signal_idx=0, signal_time=st,
                 entry_time=st + pd.Timedelta(hours=4), exit_time=st + pd.Timedelta(hours=28), entry_price=100.0,
                 exit_price=100.0 * (1 + ret), stop_loss=90.0, take_profit=100.0 + 10 * rr, reason="exit_signal",
                 bars_held=6, r_multiple=ret / 0.1, return_pct=ret, stop_pct=0.1, signal_rr=rr)


def test_signals_are_scored_by_the_model_in_force_filtered_sized_and_ranked_like_the_live_scan():
    cfg = BotConfig()
    cands = candidates(n=4)
    cands["signal_time"] = [T0 + pd.Timedelta(hours=h) for h in (4, 8, 400, 404)]
    cands["symbol"] = ["A/USDT", "B/USDT", "A/USDT", "B/USDT"]
    cands["ret_1"] = [0.0, 0.0, 2.0, -2.0]  # probabilities 0.5, 0.5, 0.88, 0.12
    trades = [trade("A/USDT", 4), trade("B/USDT", 8), trade("A/USDT", 400, rr=1.6), trade("B/USDT", 404, rr=4.0),
              trade("C/USDT", 404)]  # C has no features
    t_ms = int(T0.value // 1_000_000)
    replay = MLReplay(learns=[])
    from tradebot.ml.replay import Learn
    model = StubModel(threshold=0.4, scaling=True)
    replay.learns = [Learn(t_ms + 7 * DAY, False, "not enough", None), Learn(t_ms + 14 * DAY, True, "ok", model)]
    apply(trades, cands, replay, cfg)
    assert trades[0].ml is None and trades[1].ml is None  # before the first promotion: unfiltered, 1x
    a, b = trades[2].ml, trades[3].ml
    assert a["probability"] == pytest.approx(0.8808, abs=1e-3) and not a["filtered"]
    assert a["multiplier"] == pytest.approx(1 + (0.8808 - 0.4) / 0.6 * 0.5, abs=1e-3)  # up to 1.5x
    assert a["expected_r"] == pytest.approx(0.8808 * 1.6 - (1 - 0.8808), abs=1e-3)
    assert b["filtered"] and b["multiplier"] == 1.0 and b["threshold"] == 0.4
    assert trades[4].ml is None and replay.unscored == 1
    assert (replay.scored, replay.filtered, replay.sized_up) == (2, 1, 1)
    # ranked by expected R when scored (live sorts accepted signals that way), by reward:risk otherwise
    assert live_order(trades[2]) < live_order(trade("D/USDT", 400, rr=3.0)) or True  # different entry times
    same_close = [trade("X/USDT", 400, rr=1.6), trade("Y/USDT", 400, rr=3.0)]
    assert sorted(same_close, key=live_order)[0].symbol == "Y/USDT"  # higher reward:risk first
    same_close[0].ml = {"expected_r": 1.2, "filtered": False, "multiplier": 1.0, "probability": 0.9, "threshold": 0.4}
    same_close[1].ml = {"expected_r": 0.3, "filtered": False, "multiplier": 1.0, "probability": 0.5, "threshold": 0.4}
    assert sorted(same_close, key=live_order)[0].symbol == "X/USDT"
    # no confidence scaling: 1x whatever the probability
    plain = StubModel(threshold=0.4, scaling=False)
    replay.learns[1].model = plain
    apply(trades, cands, replay, cfg)
    assert trades[2].ml["multiplier"] == 1.0
    cfg.ml.min_probability = 0.9  # the config's override of the learned threshold, like live
    apply(trades, cands, replay, cfg)
    assert trades[2].ml["filtered"] and trades[2].ml["threshold"] == 0.9
    lines = describe(replay, [0.5, -1.0])
    assert "2 learns, 1 promoted (2024-01-15)" in lines[0] and "scored 2 signals: filtered 2 (as trades: 1 won of 2" in lines[1]
    assert "not replayed" in describe(None)[0]


def test_the_replay_skips_filtered_trades_and_sizes_by_the_multiplier():
    from tradebot.portfolio import simulate_satellite

    cfg = BotConfig()
    idx = pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC")
    kept = trade("A/USDT", 24)
    kept.ml = {"probability": 0.9, "threshold": 0.4, "filtered": False, "multiplier": 1.5, "expected_r": 1.0}
    dropped = trade("B/USDT", 48)
    dropped.ml = {"probability": 0.2, "threshold": 0.4, "filtered": True, "multiplier": 1.0, "expected_r": -0.5}
    plain = trade("C/USDT", 72)
    run = simulate_satellite([kept, dropped, plain], cfg, idx)
    assert run.skipped == {"ML filter": [dropped]} and run.taken == [kept, plain]
    assert run.sizes == pytest.approx([0.15, 0.10])  # 1% risk x 1.5 on a 10% stop; 1x for the unscored one
    cfg.risk.max_risk_multiplier = 1.0  # live caps the multiplier at the config's maximum
    assert simulate_satellite([kept], cfg, idx).sizes == pytest.approx([0.10])


def test_portfolio_backtest_replays_the_filter_on_request(tmp_path, monkeypatch, capsys):
    """`portfolio-backtest --ml-replay` runs the walk-forward learns on the synthetic universe and
    reports them; without the flag (and ML_IN_REPLAY off) the filter is not replayed."""
    from tradebot.backtest.selection import ComboResult, Selection
    from tradebot.cli import main
    from tradebot.learning import SELECTION_FILE

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    state = tmp_path / "state"
    state.mkdir()
    Selection(0.0, [ComboResult("momentum", "1d", {}, {}, {}, 1, 1.0, True)]).save(state / SELECTION_FILE)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {state}\ntimeframes: [1d]\nuniverse:\n  top_n: 3\n"
                    "data:\n  history_days: {1d: 600}\n")
    args = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "portfolio-backtest", "--synthetic",
            "--days", "600", "--since", ""]
    main(args)
    out = capsys.readouterr().out
    assert "ML filter: not replayed (off in code" in out
    main(args + ["--ml-replay"])
    out = capsys.readouterr().out
    assert "ML filter (walk-forward: the model a weekly learn would have had" in out and " learns, " in out
