from pathlib import Path

import pytest
import yaml

from tradebot.config import load_config
from tradebot.config_edit import config_set, edit_text

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ["costs.fee_rate=0.00075", "costs.maker_fee_rate=0.00075", "costs.entry_order=market",
            "selection.btc_filter=auto", "core.fraction=0.65", "guards.vol_breaker=false", "dashboard.serve=true"]


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("TRADEBOT_MODE", raising=False)


def test_edits_only_the_named_lines_and_keeps_comments(tmp_path):
    p = tmp_path / "config.yaml"
    original = (ROOT / "config.example.yaml").read_text()
    p.write_text(original)
    report = config_set(p, SETTINGS)
    new = p.read_text()
    cfg = load_config(p, env_file=None)
    assert cfg.costs.fee_rate == 0.00075 and cfg.costs.maker_fee_rate == 0.00075
    assert cfg.costs.entry_order == "market" and cfg.selection.btc_filter == "auto"
    assert cfg.core.fraction == 0.65 and cfg.guards.vol_breaker is False and cfg.dashboard.serve is True
    changed = [(a, b) for a, b in zip(original.splitlines(), new.splitlines()) if a != b]
    assert len(new.splitlines()) == len(original.splitlines())
    assert {b.split(":")[0].strip() for a, b in changed} <= {"fee_rate", "maker_fee_rate", "fraction", "serve"}
    assert "# taker fee per side" in new and new.count("#") == original.count("#")  # comments kept
    assert any("core.fraction: 0 -> 0.65" in line for line in report)
    backups = list(tmp_path.glob("config.yaml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original


def test_adds_missing_keys_and_sections(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("mode: paper  # comment\ncosts:\n  slippage_rate: 0.0005\n\n# trailing note\n")
    config_set(p, ["costs.fee_rate=0.00075", "core.fraction=0.65", "name=A"])
    data = yaml.safe_load(p.read_text())
    assert data == {"mode": "paper", "costs": {"slippage_rate": 0.0005, "fee_rate": 0.00075},
                    "core": {"fraction": 0.65}, "name": "A"}
    assert "# comment" in p.read_text() and "# trailing note" in p.read_text()


def test_inline_sections_fall_back_to_a_rewrite_with_the_same_values(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("costs: {fee_rate: 0.001, slippage_rate: 0.0005}  # inline\n")
    report = config_set(p, ["costs.fee_rate=0.00075"])
    assert yaml.safe_load(p.read_text()) == {"costs": {"fee_rate": 0.00075, "slippage_rate": 0.0005}}
    assert any("comments were dropped" in line for line in report)


def test_invalid_values_and_unknown_keys_leave_the_file_alone(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("costs:\n  fee_rate: 0.001\n")
    for bad in (["costs.entry_order=sometimes"], ["costs.fee_ratee=0.1"], ["core.fraction=1.5"], ["nonsense"]):
        with pytest.raises(ValueError):
            config_set(p, bad)
    assert p.read_text() == "costs:\n  fee_rate: 0.001\n"
    assert not list(tmp_path.glob("*.tmp")) and not list(tmp_path.glob("*.bak-*"))


def test_edit_text_never_changes_meaning():
    text = "a:\n  b: 1   # one\n  c:\n    d: x\ne: [1, 2]\n"
    new, kept = edit_text(text, {"a.c.d": "y", "e": [3], "a.b": 2})
    assert kept and yaml.safe_load(new) == {"a": {"b": 2, "c": {"d": "y"}}, "e": [3]} and "# one" in new
    new, kept = edit_text(text, {"a.c": 5})  # a section replaced by a value: rewritten, same meaning
    assert yaml.safe_load(new) == {"a": {"b": 1, "c": 5}, "e": [1, 2]}


def test_cli_config_set(tmp_path):
    from tradebot.cli import main

    p = tmp_path / "config.yaml"
    p.write_text(f"state_dir: {tmp_path / 'state'}\n")
    main(["--config", str(p), "--env", str(tmp_path / "none.env"), "config-set", "core.fraction=0.65"])
    assert load_config(p, env_file=None).core.fraction == 0.65
