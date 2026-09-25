import pytest

from tradebot.config import RiskConfig
from tradebot.models import Position, Signal
from tradebot.risk import RiskManager


def sig(symbol="BTC/USDT", entry=100.0, sl=95.0, tp=110.0):
    return Signal(symbol, "1h", "trend", "long", entry, sl, tp, 0, 0, 0, 0)


def pos(symbol):
    return Position(symbol, "1h", "trend", "long", "paper", 1.0, 100, 95, 110, 95, 0, 0)


def test_size_risks_fixed_fraction():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=30))
    d = rm.size(1000, 100, 95)
    assert d.ok
    assert d.amount == pytest.approx(2.0)  # $10 risk / $5 per coin
    assert d.risk_amount == pytest.approx(10.0)


def test_size_capped_by_position_limit_and_cash():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=30))
    d = rm.size(1000, 100, 99.5)  # tight stop would want $2000 notional
    assert d.notional == pytest.approx(300.0)
    d = rm.size(1000, 100, 99.5, available_cash=100)
    assert d.notional == pytest.approx(98.0)


def test_size_respects_exposure_and_minimums():
    rm = RiskManager(RiskConfig(max_total_exposure_pct=100))
    assert not rm.size(1000, 100, 95, open_notional=1000).ok
    d = rm.size(100, 100, 95, limits={"min_cost": 10})  # $1 risk / $5 stop -> $20 position: fine
    assert d.ok and d.notional == pytest.approx(20.0)
    d = rm.size(100, 100, 95, limits={"min_cost": 50})
    assert not d.ok and "minimum" in d.reason
    d = rm.size(1000, 100, 95, to_precision=lambda a: round(a, 0))
    assert d.amount == 2.0


def test_entry_blocks():
    rm = RiskManager(RiskConfig(max_open_positions=2, min_reward_risk=1.5))
    assert rm.entry_block_reason(sig(), []) is None
    assert "already" in rm.entry_block_reason(sig(), [pos("BTC/USDT")])
    assert "max open" in rm.entry_block_reason(sig(), [pos("A/USDT"), pos("B/USDT")])
    assert "reward:risk" in rm.entry_block_reason(sig(tp=104), [])


def test_circuit_breakers():
    rm = RiskManager(RiskConfig(daily_loss_limit_pct=3, max_drawdown_pct=15))
    assert not rm.daily_limit_hit(980, 1000)
    assert rm.daily_limit_hit(969, 1000)
    assert not rm.drawdown_hit(900, 1000)
    assert rm.drawdown_hit(849, 1000)
