import numpy as np
import pandas as pd
import pytest

from tradebot.config import BotConfig
from tradebot.data.synthetic import generate_ohlcv


@pytest.fixture(scope="session")
def ohlcv_1h() -> pd.DataFrame:
    return generate_ohlcv(3000, "1h", seed=11)


@pytest.fixture
def cfg(tmp_path) -> BotConfig:
    c = BotConfig(state_dir=str(tmp_path / "state"))
    c.data.dir = str(tmp_path / "data")
    return c


def bars(rows, start="2024-01-01", freq="1h") -> pd.DataFrame:
    """Build an OHLCV frame from (open, high, low, close) tuples."""
    arr = np.asarray(rows, dtype=float)
    idx = pd.date_range(start, periods=len(arr), freq=freq, tz="UTC", name="time")
    return pd.DataFrame({"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2], "close": arr[:, 3],
                         "volume": np.full(len(arr), 1000.0)}, index=idx)
