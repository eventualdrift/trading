import numpy as np
import pandas as pd
import pytest

from tradebot import oos


class FakeBitstamp:
    """Daily BTC/USD candles 2012-01-01..2017-08-16 with a gap, zero-volume days, one bad print and a real crash."""

    def __init__(self, seed=1, zero_volume_days=2):
        idx = pd.date_range("2012-01-01", "2017-08-16", freq="D", tz="UTC")
        rng = np.random.default_rng(seed)
        close = 5 * np.cumprod(1 + rng.normal(0.002, 0.04, len(idx)))
        vol = np.full(len(idx), 1000.0)
        df = pd.DataFrame({"open": close, "high": close * 1.02, "low": close * 0.98, "close": close, "volume": vol},
                          index=idx)
        df = df.drop(idx[300:304])  # 4-day outage
        df.loc[idx[500:500 + zero_volume_days], "volume"] = 0.0
        df.loc[idx[700], "close"] = df.loc[idx[699], "close"] * 3  # bad print, back next day
        df.loc[idx[701], "close"] = df.loc[idx[699], "close"] * 1.01
        df.loc[idx[900], "close"] = df.loc[idx[899], "close"] * 0.5  # a real crash that stays down
        df.loc[idx[901]:, ["open", "high", "low", "close"]] *= 0.5
        self.df = df

    def history(self, symbol, tf, start_ms, end_ms=None):
        assert symbol == "BTC/USD" and tf == "1d"
        return self.df


def test_cleaning_rule_fills_gaps_zero_volume_and_reversed_spikes_only(tmp_path):
    path = oos.fetch(FakeBitstamp(), tmp_path)
    closes, filled = oos.clean(oos.load_raw(path))
    reasons = filled.groupby("reason").size().to_dict()
    assert reasons == {"no candle": 4, "zero volume": 2, "bad print (spike reversed next day)": 1}
    day = pd.Timestamp("2012-01-01", tz="UTC")
    assert closes[day + pd.Timedelta(days=700)] == closes[day + pd.Timedelta(days=699)]  # spike replaced
    assert closes[day + pd.Timedelta(days=900)] < 0.6 * closes[day + pd.Timedelta(days=899)]  # real crash kept
    assert closes.index[-1] == oos.END and closes.index.is_monotonic_increasing


def test_quality_summary_has_no_rule_results(tmp_path):
    path = oos.fetch(FakeBitstamp(), tmp_path)
    text = oos.quality_summary(path)
    assert "SHA-256" in text and "4 day(s) from" in text and "Zero-volume days: 2" in text
    assert "Bad print replaced" in text and "usable under the 5% limit" in text
    assert "Sharpe" not in text and "VERDICT" not in text and "per year" not in text


def test_order_is_enforced_and_the_test_runs_once(tmp_path):
    state, data = tmp_path / "state", tmp_path / "data"
    with pytest.raises(RuntimeError, match="fetch the data first"):
        oos.register(state, data, "abc")
    oos.fetch(FakeBitstamp(), data)
    with pytest.raises(RuntimeError, match="not registered"):
        oos.run(state, data, "abc")
    reg = oos.register(state, data, "abc")
    with pytest.raises(RuntimeError, match="already registered"):
        oos.register(state, data, "abc")
    res = oos.run(state, data, "abc")
    assert res["verdict"] in ("pass", "dip_only", "fail") and res["data_sha256"] == reg["data_sha256"]
    assert res["reading"] == oos.TEST["reading"][res["verdict"]]
    assert res["stats"]["core"]["trades"] > 0 and res["measured_from"] < res["trading_period"][0]
    with pytest.raises(RuntimeError, match="already ran"):
        oos.run(state, data, "abc")
    text = oos.format_result(oos.result(state))
    assert "buy-and-hold BTC" in text and "VERDICT" in text
    kinds = [__import__("json").loads(x)["kind"] for x in (state / "research" / "ledger.jsonl").read_text().splitlines()]
    assert kinds == ["registration", "result"]


def test_run_refuses_data_other_than_the_registered_file(tmp_path):
    state, data = tmp_path / "state", tmp_path / "data"
    oos.fetch(FakeBitstamp(seed=1), data)
    oos.register(state, data, "abc")
    oos.fetch(FakeBitstamp(seed=2), data)  # re-fetched, different numbers
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        oos.run(state, data, "abc")


def test_unusable_data_gets_no_verdict(tmp_path):
    state, data = tmp_path / "state", tmp_path / "data"
    oos.fetch(FakeBitstamp(zero_volume_days=300), data)  # far more than 5% of the trading period
    oos.register(state, data, "abc")
    res = oos.run(state, data, "abc")
    assert res["verdict"] == "unusable" and "stats" not in res


def test_cli_register_run_show(tmp_path, monkeypatch, capsys):
    from tradebot.cli import main

    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    conf = tmp_path / "c.yaml"
    conf.write_text(f"state_dir: {tmp_path / 'state'}\ndata:\n  dir: {tmp_path / 'data'}\n")
    base = ["--config", str(conf), "--env", str(tmp_path / "none.env"), "research"]
    with pytest.raises(SystemExit):
        main(base + ["oos-run"])  # nothing registered
    oos.fetch(FakeBitstamp(), tmp_path / "data")
    main(base + ["oos-register"])
    out = capsys.readouterr().out
    assert "Protocol core-btc-pre2017-v1" in out and "Registered" in out and "0.5% per side" in out
    main(base + ["oos-run"])
    assert "VERDICT" in capsys.readouterr().out
    main(base + ["oos-show"])
    assert "VERDICT" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(base + ["oos-run"])  # once only
