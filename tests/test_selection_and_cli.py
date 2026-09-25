import pytest

from tradebot.backtest.selection import Selection, run_selection
from tradebot.cli import main
from tradebot.data.synthetic import generate_ohlcv


def test_selection_runs_and_roundtrips(cfg, tmp_path):
    cfg.selection.min_trades_in_sample = 5
    cfg.selection.min_trades_out_of_sample = 2
    datasets = {"1h": {f"S{i}/USDT": generate_ohlcv(4000, "1h", seed=i) for i in range(3)}}
    sel = run_selection(datasets, cfg, log=lambda *_: None)
    assert len(sel.combos) == 3
    for c in sel.combos:
        assert c.selected == (not c.reasons)
        assert c.in_sample["trades"] + c.out_of_sample["trades"] > 0
    path = tmp_path / "sel.json"
    sel.save(path)
    again = Selection.load(path)
    assert [c.key for c in again.combos] == [c.key for c in sel.combos]
    assert again.timeframes() == sel.timeframes()


def test_trades_straddling_the_split_are_purged(cfg):
    """Review #11: an in-sample trade must not use out-of-sample prices."""
    from tradebot.backtest.selection import run_combo
    from tradebot.strategies import make_strategy

    df = generate_ohlcv(4000, "1h", seed=2)
    is_t, oos_t, _ = run_combo({"A/USDT": df}, "breakout", {}, "1h", cfg)
    warmup = make_strategy("breakout").warmup
    split = warmup + int((len(df) - warmup) * cfg.selection.in_sample_fraction)
    assert is_t and oos_t
    assert all(t.exit_time < df.index[split] for t in is_t)
    assert all(t.signal_idx >= split for t in oos_t)


def test_symbol_score_uses_only_admitted_trades(cfg):
    from tradebot.backtest.selection import run_combo

    df = generate_ohlcv(4000, "1h", seed=2)
    is_t, oos_t, per_symbol = run_combo({"A/USDT": df}, "breakout", {}, "1h", cfg)
    admitted = is_t + oos_t
    assert per_symbol["A/USDT"] == pytest.approx(sum(t.r_multiple for t in admitted) / len(admitted))


def test_demo_runs_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["demo", "--days", "240", "--sim-days", "4", "--symbols-n", "2"])
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "Go-live readiness" in out
