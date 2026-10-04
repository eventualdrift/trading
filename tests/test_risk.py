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


def test_risk_multiplier_scales_and_is_capped():
    rm = RiskManager(RiskConfig(risk_per_trade_pct=1.0, max_position_pct=100, max_risk_multiplier=1.5))
    assert rm.size(1000, 100, 95, risk_multiplier=1.5).risk_amount == pytest.approx(15.0)
    assert rm.size(1000, 100, 95, risk_multiplier=3.0).risk_amount == pytest.approx(15.0)  # capped
    assert rm.size(1000, 100, 95, risk_multiplier=0.2).risk_amount == pytest.approx(10.0)  # never below base


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


def test_open_risk_budget_counts_only_risk_still_at_stake():
    from tradebot.config import RiskConfig
    from tradebot.models import Position, Signal
    from tradebot.risk import RiskManager

    rm = RiskManager(RiskConfig(max_open_risk_pct=3.0))

    def pos(sym, stop):  # 10 coins bought at 100: 1% of a 10,000 account at risk with a stop at 90
        return Position(symbol=sym, timeframe="1d", strategy="momentum", side="long", mode="paper", amount=10,
                        entry_price=100, stop_loss=stop, take_profit=200, initial_stop=90, opened_at=0,
                        max_hold_until=10**13)

    sig = Signal(symbol="NEW/USDT", timeframe="1d", strategy="breakout", side="long", entry=50, stop_loss=45,
                 take_profit=70, candle_time=0, created_at=0, valid_until=1, max_hold_until=1)
    fresh = [pos("A/USDT", 90), pos("B/USDT", 90)]
    assert rm.entry_block_reason(sig, fresh, 10_000) is None  # 2% at stake + 1% new = 3%
    assert "open-risk budget" in rm.entry_block_reason(sig, fresh + [pos("C/USDT", 90)], 10_000)
    protected = [pos(s, 100) for s in ("C/USDT", "D/USDT", "E/USDT", "F/USDT")]  # stops at entry: 0 at stake
    assert rm.entry_block_reason(sig, fresh + protected, 10_000) is None  # 6 open, still allowed
    rm.cfg.max_open_risk_pct = None  # off: the position count applies again
    assert "max open positions" in rm.entry_block_reason(sig, fresh + protected, 10_000)


def test_go_live_needs_every_protective_exit_on_the_exchange(monkeypatch):
    import tradebot.execution.live as live
    from tradebot.report import exchange_protection_check

    check = exchange_protection_check()
    assert not check.passed and "breakeven stop, trailing stop, take profit" in check.detail
    monkeypatch.setattr(live, "EXCHANGE_SIDE_EXITS",
                        frozenset({"stop_loss", "breakeven_stop", "trailing_stop", "take_profit"}))
    assert exchange_protection_check().passed
