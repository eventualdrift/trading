import io
import zipfile

import pandas as pd
import pytest

from tradebot import pit_universe as pit
from tradebot.data import OHLCVStore


def _zip(lines: list[str], name: str = "X-1d-2025-01.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


def _row(ts: int, close: float, qv: float, rng: float = 0.05) -> str:
    return f"{ts},{close},{close * (1 + rng)},{close * (1 - rng)},{close},10,{ts + 86_399_999},{qv},5,1,1,0"


def _daily(start: str, days: int, qv, close: float = 10.0, rng: float = 0.05) -> pd.DataFrame:
    idx = pd.date_range(start, periods=days, freq="D", tz="UTC", name="time")
    qv = qv if isinstance(qv, list) else [qv] * days
    return pd.DataFrame({"open": close, "high": close * (1 + rng), "low": close * (1 - rng), "close": close,
                         "volume": 10.0, "quote_volume": qv}, index=idx)


def test_a_monthly_file_parses_to_daily_volume_in_milliseconds_or_microseconds():
    jan = int(pd.Timestamp("2025-01-01", tz="UTC").value // 1_000_000)
    df = pit.parse_month(_zip([_row(jan, 2.0, 7e6), _row((jan + 86_400_000) * 1000, 3.0, 8e6)]))
    assert list(df.index.strftime("%Y-%m-%d")) == ["2025-01-01", "2025-01-02"]  # the second in microseconds
    assert list(df["quote_volume"]) == [7e6, 8e6] and list(df["close"]) == [2.0, 3.0]
    assert list(df.columns) == pit.COLUMNS


def test_the_rule_on_each_day_uses_the_day_befores_volume_and_drops_delisted_coins():
    daily = {
        "OLD/USDT": _daily("2020-01-01", 900, 9e6),
        "BIG/USDT": _daily("2020-01-01", 900, 2e6),  # under the 5M floor
        "GONE/USDT": _daily("2020-01-01", 800, 8e6),  # stops trading on 2022-03-10
        "NEW/USDT": _daily("2021-06-01", 383, 20e6),  # listed later: in only after a year
        "USDC/USDT": _daily("2020-01-01", 900, 50e6),  # a stablecoin
        "BTCUP/USDT": _daily("2020-01-01", 900, 50e6),  # a leveraged token
        "PEG/USDT": _daily("2020-01-01", 900, 50e6, close=1.0, rng=0.001),  # pegged: a 0.2% range
    }
    spike = [6e6] * 900
    spike[(pd.Timestamp("2021-12-31") - pd.Timestamp("2020-01-01")).days] = 1e9  # SPK's volume jumps on 12-31 only
    daily["SPK/USDT"] = _daily("2020-01-01", 900, spike)
    u = pit.build(daily, top_n=2, min_quote_volume=5e6, min_history_days=365, start="2021-12-31", end="2022-06-02")
    days = u["days"]
    assert days["2021-12-31"] == ["OLD/USDT", "GONE/USDT"]  # ranked on 12-30: SPK's spike isn't known yet
    assert days["2022-01-01"] == ["SPK/USDT", "OLD/USDT"]  # ranked on 12-31
    assert days["2022-01-02"] == ["OLD/USDT", "GONE/USDT"]
    assert days["2022-03-11"] == ["OLD/USDT", "GONE/USDT"]  # its last candle, 03-10, still ranks it
    assert days["2022-03-12"] == ["OLD/USDT", "SPK/USDT"]
    assert days["2022-06-01"] == ["NEW/USDT", "OLD/USDT"]  # a year after its first candle (2021-06-01)
    assert days["2022-05-31"] == ["OLD/USDT", "SPK/USDT"]
    assert not any(p in v for v in days.values() for p in ("USDC/USDT", "BTCUP/USDT", "PEG/USDT", "BIG/USDT"))
    assert u["listed"]["GONE/USDT"] == [["2020-01-01", "2022-03-10"]] and u["reused"] == {}

    rows = {r["year"]: r for r in pit.survivorship(u, today=["OLD/USDT", "NEW/USDT"])}
    assert rows["2022"]["coins"] == 4 and rows["2022"]["gone"] == 1 and rows["2022"]["not_today"] == 2
    assert rows["2021"]["gone_share"] == pytest.approx(0.5)  # one of two members on 2021-12-31
    assert "stopped trading since" in pit.format_survivorship(list(rows.values()), "the reference's list")


def test_fetch_keeps_the_months_and_fetches_only_new_ones(tmp_path, monkeypatch):
    store = OHLCVStore(tmp_path, "binance-archive")
    months = {"2025-01": "2025-01-01", "2025-02": "2025-02-01", "2025-03": "2025-03-01"}
    got = []

    def listing(prefix, delimiter=""):
        return [f"{prefix}ABCUSDT-1d-{m}.zip" for m in months] + [f"{prefix}ABCUSDT-1d-2025-01.zip.CHECKSUM"]

    def get(url, timeout=60):
        m = url.rsplit("-1d-", 1)[1][:7]
        got.append(m)
        ts = int(pd.Timestamp(months[m], tz="UTC").value // 1_000_000)
        return _zip([_row(ts, 1.0, 6e6), _row(ts + 86_400_000, 1.0, 6e6)])

    monkeypatch.setattr(pit, "_list", listing)
    monkeypatch.setattr(pit, "_get", get)
    df = pit.fetch_pair(store, "ABC/USDT", "2025-02")
    assert got == ["2025-01", "2025-02"] and len(df) == 4 and len(store.load("ABC/USDT", "1d")) == 4
    got.clear()
    df = pit.fetch_pair(store, "ABC/USDT", "2025-03")
    assert got == ["2025-02", "2025-03"] and len(df) == 6  # February stopped on the 2nd: fetched again
    got.clear()
    df = pit.fetch_pair(store, "ABC/USDT", "2025-01")
    assert got == [] and len(df) == 2  # kept months after `through` are not returned


def test_archive_pairs_lists_the_quote_currencys_pairs(monkeypatch):
    names = ["BTCUSDT", "LUNAUSDT", "ETHBTC", "USDT"]
    monkeypatch.setattr(pit, "_list", lambda prefix, delimiter="": [f"{prefix}{n}/" for n in names])
    assert pit.archive_pairs("USDT") == ["BTC/USDT", "LUNA/USDT"]


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_listing_follows_every_page_and_quotes_non_ascii_names(monkeypatch):
    urls = []
    pages = {
        "": "<ListBucketResult><IsTruncated>true</IsTruncated><NextMarker>p/牛来USDT/</NextMarker>"
            "<CommonPrefixes><Prefix>p/AUSDT/</Prefix></CommonPrefixes></ListBucketResult>",
        "next": "<ListBucketResult><IsTruncated>false</IsTruncated>"
                "<CommonPrefixes><Prefix>p/牛来USDT/</Prefix></CommonPrefixes></ListBucketResult>",
    }

    def urlopen(url, timeout=60):
        url.encode("ascii")  # what urllib needs
        urls.append(url)
        return _Resp(pages["next" if "marker=" in url else ""].encode())

    monkeypatch.setattr(pit.urllib.request, "urlopen", urlopen)
    assert pit._list("p/", "/") == ["p/AUSDT/", "p/牛来USDT/"]
    assert "marker=p/%E7%89%9B%E6%9D%A5USDT/" in urls[1] and "&delimiter=/" in urls[0]
    # without a delimiter S3 sends no NextMarker: the last key is the marker
    keys = {"": "<IsTruncated>true</IsTruncated><Key>k/1.zip</Key><Key>k/2.zip</Key>",
            "next": "<IsTruncated>false</IsTruncated><Key>k/3.zip</Key>"}
    urls.clear()

    def urlopen_keys(url, timeout=60):
        url.encode("ascii")
        urls.append(url)
        return _Resp(keys["next" if "marker=" in url else ""].encode())

    monkeypatch.setattr(pit.urllib.request, "urlopen", urlopen_keys)
    assert pit._list("k/牛来/") == ["k/1.zip", "k/2.zip", "k/3.zip"] and "marker=k/2.zip" in urls[1]
    assert "prefix=k/%E7%89%9B%E6%9D%A5/" in urls[0]


def test_a_server_error_is_retried_and_a_missing_file_is_not(monkeypatch):
    calls = []

    def urlopen(url, timeout=60):
        calls.append(url)
        if url.endswith("flaky") and len(calls) < 3:
            raise pit.urllib.error.HTTPError(url, 502, "Bad Gateway", {}, None)
        if url.endswith("gone"):
            raise pit.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Resp(b"ok")

    monkeypatch.setattr(pit.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(pit.time, "sleep", lambda s: None)
    assert pit._get("https://x/flaky") == b"ok" and len(calls) == 3
    calls.clear()
    with pytest.raises(pit.urllib.error.HTTPError):
        pit._get("https://x/gone")
    assert len(calls) == 1


def test_a_pair_that_still_fails_stops_the_build_with_its_name(monkeypatch):
    def fetch(store, pair, through):
        if pair == "BAD/USDT":
            raise OSError("boom")
        return _daily("2025-01-01", 3, 6e6)

    monkeypatch.setattr(pit, "fetch_pair", fetch)
    with pytest.raises(RuntimeError, match="1 pairs could not be fetched.*BAD/USDT: boom"):
        pit.fetch_all(None, ["A/USDT", "BAD/USDT"], "2025-01", log_fn=lambda *_: None)


def test_like_live_only_the_top_2n_are_checked_for_age_and_a_relisted_coin_needs_a_candle_at_the_cutoff():
    daily = {f"Y{i}/USDT": _daily("2021-06-01", 600, 50e6 - i) for i in range(3)}  # young: listed a year later
    daily.update({"OLD/USDT": _daily("2020-01-01", 1200, 7e6), "OLD2/USDT": _daily("2020-01-01", 1200, 6e6)})
    back = _daily("2020-01-01", 1200, 9e6)  # delisted 2020-11-01 to 2021-06-30, then traded again
    daily["BACK/USDT"] = back[(back.index < "2020-11-01") | (back.index > "2021-06-30")]
    daily["AUD/USDT"] = _daily("2020-01-01", 1200, 99e6, close=0.7, rng=0.01)  # a fiat ticker live no longer lists
    u = pit.build(daily, top_n=2, min_quote_volume=5e6, min_history_days=365, start="2021-07-02", end="2021-07-02")
    assert u["days"]["2021-07-02"] == ["BACK/USDT"]  # the top 4: Y0-Y2 (young) and BACK: fewer than 2, as live
    u = pit.build(daily, top_n=3, min_quote_volume=5e6, min_history_days=365, start="2021-07-02", end="2021-07-02")
    assert u["days"]["2021-07-02"] == ["BACK/USDT", "OLD/USDT", "OLD2/USDT"]  # the top 6 reach the old coins
    u = pit.build(daily, top_n=3, min_quote_volume=5e6, min_history_days=365, start="2021-11-02", end="2021-12-01")
    assert u["days"]["2021-11-02"] == ["BACK/USDT", "OLD/USDT", "OLD2/USDT"]  # a candle on 2020-10-31, near the cutoff
    assert u["days"]["2021-12-01"] == ["OLD/USDT", "OLD2/USDT"]  # no candle around 2020-12-01: a new listing to live
    assert u["listed"]["BACK/USDT"] == [["2020-01-01", "2020-10-31"], ["2021-07-01", "2023-04-14"]]


def test_renamed_and_reused_tickers_in_the_survivorship_count():
    daily = {"MATIC/USDT": _daily("2020-01-01", 400, 9e6), "POL/USDT": _daily("2021-02-04", 300, 9e6),
             "KEEP/USDT": _daily("2020-01-01", 700, 8e6),
             "LUNA/USDT": pd.concat([_daily("2020-01-01", 400, 7e6, close=80.0),
                                     _daily("2021-02-20", 300, 7e6, close=0.5)])}
    u = pit.build(daily, top_n=3, min_quote_volume=5e6, min_history_days=0, start="2020-06-01", end="2021-11-30")
    assert u["reused"] == {"LUNA/USDT": ["2021-02-03..2021-02-20"]}  # 80 -> 0.5 across a 17-day gap
    rows = {r["year"]: r for r in pit.survivorship(u)}
    assert rows["2020"]["renamed"] == 1 and rows["2020"]["gone"] == 1  # MATIC renamed; Terra-like LUNA stopped
    assert rows["2020"]["gone_days"] == (pd.Timestamp("2020-12-31") - pd.Timestamp("2020-06-01")).days + 1
    assert rows["2021"]["gone"] == 1 and rows["2021"]["renamed"] == 1
