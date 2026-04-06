"""Tests for CLI config merge logic (parse_with_config)."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from librae.cli import base_parser, parse_with_config


@pytest.fixture()
def _clear_argv(monkeypatch):
    """Strip pytest args so argparse sees a clean argv."""
    monkeypatch.setattr(sys, "argv", ["test"])


@pytest.fixture()
def config_yaml(tmp_path):
    """Write a temporary config YAML and return its path."""
    def _write(content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(textwrap.dedent(content))
        return p
    return _write


@pytest.mark.usefixtures("_clear_argv")
class TestParseWithConfig:
    """parse_with_config: YAML defaults + CLI overrides + structured keys."""

    def test_no_config_uses_argparse_defaults(self):
        p = base_parser("test")
        ns = parse_with_config(p, config_path=None)
        assert ns.mode == "backtest"
        assert ns.poll_seconds == 60

    def test_yaml_scalars_become_argparse_defaults(self, config_yaml):
        cfg = config_yaml("""\
            mode: sim
            poll-seconds: 30
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.mode == "sim"
        assert ns.poll_seconds == 30

    def test_cli_overrides_yaml(self, config_yaml, monkeypatch):
        cfg = config_yaml("""\
            mode: sim
            poll-seconds: 30
        """)
        monkeypatch.setattr(sys, "argv", ["test", "--mode", "live"])
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.mode == "live"          # CLI wins
        assert ns.poll_seconds == 30      # YAML default kept

    def test_structured_keys_attached_as_dict(self, config_yaml):
        cfg = config_yaml("""\
            mode: sim
            telegram:
              enabled: true
              notifications:
                signal: false
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.mode == "sim"
        assert isinstance(ns.telegram, dict)
        assert ns.telegram["enabled"] is True
        assert ns.telegram["notifications"]["signal"] is False

    def test_structured_keys_not_in_argparse(self, config_yaml):
        """Dict-valued YAML keys should not leak into argparse defaults."""
        cfg = config_yaml("""\
            telegram:
              enabled: true
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        # telegram is a setattr, not an argparse-registered arg
        assert ns.telegram == {"enabled": True}

    def test_missing_config_file_warns(self, tmp_path, caplog):
        p = base_parser("test")
        ns = parse_with_config(p, config_path=tmp_path / "nonexistent.yaml")
        assert ns.mode == "backtest"      # falls back to argparse default
        assert "Config file not found" in caplog.text

    def test_cli_config_flag_overrides_config_path(self, config_yaml, monkeypatch):
        """--config on CLI takes precedence over config_path argument."""
        default_cfg = config_yaml("""\
            mode: sim
        """)
        override = default_cfg.parent / "override.yaml"
        override.write_text("mode: live\n")

        monkeypatch.setattr(sys, "argv", ["test", "--config", str(override)])
        p = base_parser("test")
        ns = parse_with_config(p, config_path=default_cfg)
        assert ns.mode == "live"

    def test_multiple_structured_keys(self, config_yaml):
        cfg = config_yaml("""\
            strategy:
              name: test_strat
              timeframe: M5
            telegram:
              enabled: false
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.strategy == {"name": "test_strat", "timeframe": "M5"}
        assert ns.telegram == {"enabled": False}

    def test_empty_yaml(self, config_yaml):
        cfg = config_yaml("")
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.mode == "backtest"

    def test_hyphen_keys_converted_to_underscore(self, config_yaml):
        cfg = config_yaml("""\
            poll-seconds: 45
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.poll_seconds == 45
