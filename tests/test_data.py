from tradebot.data import OHLCVStore, SyntheticMarket
from tradebot.data.exchange import LEVERAGED, STABLECOINS, ohlcv_to_df
from tradebot.timeframes import drop_unclosed, index_ms, last_closed_open_ms, tf_ms


def test_store_update_incremental(tmp_path):
    m = SyntheticMarket(["AAA/USDT"], days=30)
    store = OHLCVStore(tmp_path, m.id)
    end = m.now_ms()
    m.set_now(end - 5 * 86_400_000)
    first = store.update(m, "AAA/USDT", "1h", 10)
    assert len(first) == 10 * 24
    m.set_now(end)
    second = store.update(m, "AAA/USDT", "1h", 10)
    assert index_ms(second.index)[-1] > index_ms(first.index)[-1]
    assert second.index.is_unique and second.index.is_monotonic_increasing
    assert len(store.load("AAA/USDT", "1h")) >= 15 * 24 - 1


def test_synthetic_market_hides_future():
    m = SyntheticMarket(2, days=20)
    now = m.start_ms + 5 * 86_400_000 + 123
    m.set_now(now)
    df = m.fetch_ohlcv_df(m.symbols[0], "1h", limit=1000)
    assert index_ms(df.index)[-1] == last_closed_open_ms(now, "1h")
    base = m.fetch_price_bars(m.symbols[0], 0)
    assert index_ms(base.index)[-1] + m.price_bar_ms <= now
    assert m.fetch_last_price(m.symbols[0]) == base["close"].iloc[-1]


def test_drop_unclosed():
    rows = [[0, 1, 1, 1, 1, 1], [3_600_000, 1, 1, 1, 1, 1]]
    df = ohlcv_to_df(rows)
    assert len(drop_unclosed(df, "1h", 3_600_000 + 10)) == 1
    assert len(drop_unclosed(df, "1h", 2 * tf_ms("1h"))) == 2


def test_spot_client_only_loads_spot_markets():
    from tradebot.data import ExchangeClient

    ex = ExchangeClient("binance", market_type="spot").ex  # no network needed to construct
    assert ex.options["fetchMarkets"]["types"] == ["spot"]  # so only api.binance.com is contacted


def test_universe_filters():
    assert "USDC" in STABLECOINS
    assert LEVERAGED.search("BTCUP") and LEVERAGED.search("ETH3L") and not LEVERAGED.search("SOL")


def test_store_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    m = SyntheticMarket(["AAA/USDT"], days=5, base_tf="1h", seed=3)
    store = OHLCVStore(tmp_path, m.id)
    df = m.fetch_ohlcv_df("AAA/USDT", "1h", limit=50)
    store.save("AAA/USDT", "1h", df)
    store.save("AAA/USDT", "1h", df)  # overwrite in place
    folder = store.path("AAA/USDT", "1h").parent
    assert [f.name for f in folder.iterdir()] == ["AAA_USDT.csv.gz"]
    assert len(store.load("AAA/USDT", "1h")) == len(df)


def test_universe_skips_stablecoins_including_unlisted_pegged_ones():
    from tradebot.data import ExchangeClient

    client = ExchangeClient("binance", market_type="spot")
    mk = lambda base: {"base": base, "quote": "USDT", "spot": True, "active": True}  # noqa: E731
    client._markets = {f"{b}/USDT": mk(b) for b in ("BTC", "RLUSD", "NEWUSD", "SOL", "ETH3L")}
    tickers = {
        "BTC/USDT": {"quoteVolume": 9e9, "high": 101_000, "low": 99_000, "last": 100_000},
        "RLUSD/USDT": {"quoteVolume": 8e9, "high": 1.0004, "low": 0.9998, "last": 1.0001},  # known stablecoin
        "NEWUSD/USDT": {"quoteVolume": 7e9, "high": 1.001, "low": 0.999, "last": 1.0},  # unknown, but pegged
        "SOL/USDT": {"quoteVolume": 6e9, "high": 210, "low": 196, "last": 205},
        "ETH3L/USDT": {"quoteVolume": 5e9, "high": 2, "low": 1, "last": 1.5},  # leveraged token
    }
    client.ex.fetch_tickers = lambda *a, **k: tickers
    assert client.top_symbols("USDT", 10) == ["BTC/USDT", "SOL/USDT"]


def test_universe_skips_new_listings_and_refills_from_established_coins():
    import pandas as pd

    from tradebot.config import BotConfig
    from tradebot.universe import DAY_MS, select_universe

    class Market:
        id = "fake"
        listed = {"AAA/USDT": 900, "NEW/USDT": 100, "BBB/USDT": 400, "CCC/USDT": 2000, "DDD/USDT": 30}
        now = 3000 * DAY_MS

        def now_ms(self):
            return self.now

        def top_symbols(self, quote, n, min_quote_volume=0.0, whitelist=None, blacklist=None):
            return list(whitelist or self.listed)[:n]  # already ranked by volume

        def history(self, symbol, tf, start_ms, end_ms=None):
            first = self.now - self.listed[symbol] * DAY_MS
            idx = pd.date_range(pd.Timestamp(max(first, start_ms), unit="ms", tz="UTC"),
                                pd.Timestamp(end_ms, unit="ms", tz="UTC"), freq="D")
            return pd.DataFrame({"close": 1.0}, index=idx)

    cfg = BotConfig()
    cfg.universe.top_n = 3
    m = Market()
    assert select_universe(m, cfg) == ["AAA/USDT", "NEW/USDT", "BBB/USDT"]  # off by default
    cfg.universe.min_history_days = 365
    notes = []
    assert select_universe(m, cfg, log_fn=notes.append) == ["AAA/USDT", "BBB/USDT", "CCC/USDT"]
    assert "NEW/USDT" in notes[0]
    cfg.universe.whitelist = ["NEW/USDT"]  # an explicit whitelist is respected as is
    assert select_universe(m, cfg) == ["NEW/USDT"]


def test_frozen_loads_never_change_the_cache(tmp_path):
    from tradebot.learning import load_frame

    m = SyntheticMarket(["AAA/USDT"], days=40, base_tf="1h", seed=3)
    store = OHLCVStore(tmp_path, m.id)
    full = store.update(m, "AAA/USDT", "1h", 30)
    before = store.path("AAA/USDT", "1h").read_bytes()
    end = int(full.index[-100].value // 1_000_000)
    frozen = load_frame(m, store, "AAA/USDT", "1h", 10, end_ms=end)
    assert frozen.index[-1] < full.index[-100]  # only candles closed by the frozen end date
    assert len(frozen) == 10 * 24 - 1 or len(frozen) == 10 * 24
    assert store.path("AAA/USDT", "1h").read_bytes() == before  # the cache still has the newer data
