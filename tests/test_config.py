from pathlib import Path

import pytest

from tradebot.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_loads(monkeypatch):
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    cfg = load_config(ROOT / "config.example.yaml", env_file=None)
    assert cfg.mode == "paper"
    assert cfg.exchange.id == "binance"
    assert cfg.risk.risk_per_trade_pct == 1.0
    assert set(cfg.strategies) == {"trend", "breakout", "meanrev"}
    assert cfg.ml.min_probability is None


def test_unknown_key_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  risk_per_trade: 2\n")
    with pytest.raises(ValueError, match="risk.risk_per_trade"):
        load_config(p, env_file=None)


def test_invalid_values_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  risk_per_trade_pct: 25\ntimeframes: [7m]\n")
    with pytest.raises(ValueError) as e:
        load_config(p, env_file=None)
    assert "risk_per_trade_pct" in str(e.value) and "7m" in str(e.value)


def test_live_mode_limited_to_verified_exchanges(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)
    p = tmp_path / "c.yaml"
    p.write_text("mode: live\nexchange:\n  id: okx\n")
    with pytest.raises(ValueError, match="binance"):
        load_config(p, env_file=None)
    p.write_text("mode: paper\nexchange:\n  id: okx\n")
    assert load_config(p, env_file=None).exchange.id == "okx"


def test_defaults_include_trend_following_and_safe_multiplier(tmp_path):
    cfg = load_config(tmp_path / "none.yaml", env_file=None)
    assert "momentum" in cfg.strategies and cfg.universe.top_n == 30
    p = tmp_path / "c.yaml"
    p.write_text("risk:\n  risk_per_trade_pct: 3\n  max_risk_multiplier: 2\n")
    with pytest.raises(ValueError, match="max_risk_multiplier"):
        load_config(p, env_file=None)  # 3% x 2 = 6% on one trade: refused


def test_secrets_from_env(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\n")
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.setenv(key, "")  # registers cleanup so .env values don't leak into other tests
        monkeypatch.delenv(key)
    monkeypatch.setenv("TRADEBOT_MODE", "paper")
    cfg = load_config(tmp_path / "missing.yaml", env_file=str(env))
    assert cfg.secrets.telegram_token == "abc" and cfg.secrets.telegram_chat_id == "42"
    assert "abc" not in repr(cfg)  # secrets never end up in logs
