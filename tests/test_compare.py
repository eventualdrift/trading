import pytest

from tradebot.bot import TradingBot
from tradebot.compare import compare_entries, config_differences, format_comparison, summarize
from tradebot.config import load_config
from tradebot.data import SyntheticMarket
from tradebot.db import Database
from tradebot.execution import PaperBroker
from tradebot.learning import SELECTION_FILE, learning_cycle, load_brain
from tradebot.notify import MemoryNotifier

from .test_bot import DAY, selection


@pytest.fixture
def pair(tmp_path, monkeypatch):
    """Instance A (market entries) and B (limit entries, following A's strategies)."""
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    (tmp_path / "a.yaml").write_text(
        f"state_dir: {tmp_path / 'a'}\nml:\n  enabled: false\ndashboard:\n  enabled: false\n")
    (tmp_path / "b.yaml").write_text(
        "extends: a.yaml\nname: B\n"
        f"state_dir: {tmp_path / 'b'}\n"
        "costs:\n  entry_order: limit\n"
        f"learning:\n  follow_state_dir: {tmp_path / 'a'}\n"
        "telegram:\n  commands: false\ndashboard:\n  port: 8766\n")
    cfg_a = load_config(tmp_path / "a.yaml", env_file=None)
    cfg_b = load_config(tmp_path / "b.yaml", env_file=None)
    selection(("breakout", "1h"), ("trend", "4h")).save(cfg_a.state_path / SELECTION_FILE)
    market = SyntheticMarket(4, days=120, seed=3)
    bots = []
    for cfg in (cfg_a, cfg_b):
        db = Database(cfg.state_path / "tradebot.db")
        sel, model = load_brain(cfg)
        broker = PaperBroker(db, cfg.costs_model(), 1000.0, market)
        bots.append(TradingBot(cfg, market, broker, db, MemoryNotifier(), selection=sel, model=model))
    return market, cfg_a, cfg_b, bots[0], bots[1]


def run_both(market, bots, start, days):
    step = market.price_bar_ms
    t = (start // step + 1) * step
    while t < start + days * DAY:
        now = t + 30_000
        market.set_now(now)
        for bot in bots:
            bot.tick(now)
        t += step


def test_config_extends_and_differences(pair):
    market, cfg_a, cfg_b, a, b = pair
    assert cfg_b.ml.enabled is False  # inherited from a.yaml
    assert cfg_b.costs.entry_order == "limit" and cfg_b.name == "B"
    assert config_differences(cfg_a, cfg_b) == []
    cfg_b.risk.max_open_positions = 5
    assert any("risk.max_open_positions" in d for d in config_differences(cfg_a, cfg_b))


def test_extends_loop_and_missing_file(tmp_path):
    (tmp_path / "x.yaml").write_text("extends: y.yaml\n")
    (tmp_path / "y.yaml").write_text("extends: x.yaml\n")
    with pytest.raises(ValueError, match="loop"):
        load_config(tmp_path / "x.yaml", env_file=None)
    (tmp_path / "z.yaml").write_text("extends: nope.yaml\n")
    with pytest.raises(ValueError, match="does not exist"):
        load_config(tmp_path / "z.yaml", env_file=None)


def test_same_signals_compared_per_signal(pair):
    market, cfg_a, cfg_b, a, b = pair
    run_both(market, [a, b], market.start_ms + 45 * DAY, 20)
    rows = compare_entries(a.db, b.db)
    assert rows
    # B follows A's strategies, so every signal exists in both instances
    assert not [r for r in rows if "no signal" in (r.a_state, r.b_state)]
    s = summarize(rows)
    assert s["comparable"] > 0 and s["filled"] + s["missed"] > 0
    assert 0.0 <= s["fill_rate"] <= 1.0
    for r in rows:
        if r.b_state == "filled":
            assert r.b_fill == pytest.approx(r.signal_price)  # limit at the signal close
        if r.improvement_bps is not None:
            sign = 1 if r.side == "long" else -1
            assert (r.improvement_bps > 0) == (sign * (r.a_fill - r.b_fill) > 0)
    text = format_comparison(rows, cfg_a, cfg_b, config_differences(cfg_a, cfg_b))
    assert "Fill rate" in text and "WARNING" not in text


def test_follower_never_learns_and_reloads_the_leaders_strategies(pair):
    market, cfg_a, cfg_b, a, b = pair
    with pytest.raises(RuntimeError, match="follows"):
        learning_cycle(cfg_b, market)
    assert "run /learn there" in b.handle_command("learn", [])
    now = market.start_ms + 50 * DAY
    market.set_now(now)
    b.tick(now)
    assert {c.key for c in b.selection.selected} == {"breakout@1h", "trend@4h"}
    selection(("momentum", "1d")).save(cfg_a.state_path / SELECTION_FILE)  # A retrains
    now += 10 * 60_000
    market.set_now(now)
    b.tick(now)
    assert {c.key for c in b.selection.selected} == {"momentum@1d"}
    assert any("Strategies reloaded" in m for m in b.notifier.messages)
