from .base import SIGNAL_COLUMNS, Strategy
from .breakout import DonchianBreakout
from .meanrev import MeanReversion
from .trend import TrendPullback

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (TrendPullback, DonchianBreakout, MeanReversion)
}


def make_strategy(name: str, params: dict | None = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise ValueError(f"Unknown strategy {name!r}; available: {sorted(STRATEGIES)}") from None
    return cls(**(params or {}))


__all__ = ["STRATEGIES", "SIGNAL_COLUMNS", "Strategy", "make_strategy"]
