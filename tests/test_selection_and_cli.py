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


def test_demo_runs_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["demo", "--days", "240", "--sim-days", "4", "--symbols-n", "2"])
    out = capsys.readouterr().out
    assert "SYNTHETIC" in out and "Go-live readiness" in out
