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


def test_universe_filters():
    assert "USDC" in STABLECOINS
    assert LEVERAGED.search("BTCUP") and LEVERAGED.search("ETH3L") and not LEVERAGED.search("SOL")
