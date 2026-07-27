"""Tests for CLI config merge logic (parse_with_config)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from librae.core.run_config import RunConfig

from orchestration.cli import (
    _resolve_market_and_data_source,
    base_parser,
    build_config,
    check_existing_run,
    parse_with_config,
)


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
        assert ns.poll_seconds is None  # no implicit default — must be set for sim/live

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
        assert ns.mode == "live"  # CLI wins
        assert ns.poll_seconds == 30  # YAML default kept

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
        assert ns.mode == "backtest"  # falls back to argparse default
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


class TestResolveMarketAndDataSource:
    """Regression tests: an unregistered symbol universe (e.g. a
    stock-picking strategy over tickers not in librae/config/symbols.py)
    used to silently fall back to market='crypto'/data_source='binance_spot'
    when config.yaml didn't set them — the run would still complete, just
    with crypto's cost/margin assumptions silently applied to equities."""

    def test_registered_symbol_infers_market(self):
        market, data_source = _resolve_market_and_data_source(["MU"], None, None)
        assert (market, data_source) == ("us_equity", "ibkr")

    def test_unregistered_symbols_without_explicit_market_raises(self):
        with pytest.raises(ValueError, match="cannot be resolved"):
            _resolve_market_and_data_source(["AAPL", "MSFT"], None, None)

    def test_unregistered_symbols_with_explicit_market_is_used(self):
        market, data_source = _resolve_market_and_data_source(["AAPL", "MSFT"], "us_equity", "ibkr")
        assert (market, data_source) == ("us_equity", "ibkr")

    def test_mixed_registered_and_unregistered_requires_explicit_route(self):
        with pytest.raises(ValueError, match="AAPL"):
            _resolve_market_and_data_source(["MU", "AAPL"], None, None)

    def test_mixed_registered_symbols_resolve_to_multi(self):
        market, data_source = _resolve_market_and_data_source(["MU", "BTCUSDT"], None, None)
        assert (market, data_source) == ("multi", "multi")

    def test_per_symbol_route_resolves_mixed_unregistered_symbols(self):
        routes = {
            "AAPL": {"market": "us_equity", "data_source": "ibkr"},
            "BTC-USD": {"market": "crypto", "data_source": "binance_spot"},
        }
        market, data_source = _resolve_market_and_data_source(
            ["AAPL", "BTC-USD"], None, None, routes
        )
        assert (market, data_source) == ("multi", "multi")


@pytest.mark.usefixtures("_clear_argv")
class TestBuildConfig:
    def test_preserves_per_symbol_cost_and_route_overrides(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbols: [AAPL]
                  timeframe: 1d
                  market: us_equity
                  data_source: ibkr
                  broker: ibkr
                  symbol_overrides:
                    AAPL:
                      multiplier: 1.0
                  instrument_overrides:
                    AAPL:
                      data_adapter: ibkr
                      currency: USD
                      security_type: STK
                """
            )
        )

        cfg = build_config("test_strat", str(tmp_path / "run.py"))

        assert cfg.symbol_overrides == {"AAPL": {"multiplier": 1.0}}
        assert cfg.broker == "ibkr"
        assert cfg.instrument_overrides == {
            "AAPL": {
                "data_adapter": "ibkr",
                "currency": "USD",
                "security_type": "STK",
            }
        }


def _make_cfg(**overrides) -> RunConfig:
    defaults = dict(
        strategy_name="test_strat",
        symbols=["MU"],
        timeframe="1d",
        market="us_equity",
        data_source="local",
        initial_balance=100_000.0,
        mode="backtest",
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


class TestCheckExistingRun:
    """check_existing_run must degrade to 'no dedup, just run it' whenever
    the DB isn't available — an unset TIMESCALE_DSN or an unreachable
    Postgres shouldn't crash a backtest that only wanted a dedup check."""

    def test_timescale_dsn_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("TIMESCALE_DSN", raising=False)
        monkeypatch.delitem(sys.modules, "db", raising=False)
        monkeypatch.delitem(sys.modules, "db.timescale_reader", raising=False)

        assert check_existing_run(_make_cfg()) is None

    def test_db_unreachable_skips_dedup_instead_of_raising(self, monkeypatch):
        monkeypatch.setenv("TIMESCALE_DSN", "postgresql://localhost:1/nonexistent")
        monkeypatch.delitem(sys.modules, "db", raising=False)
        monkeypatch.delitem(sys.modules, "db.timescale_reader", raising=False)

        with patch(
            "db.timescale_reader.get_run_by_config_hash",
            side_effect=OSError("connection refused"),
        ):
            assert check_existing_run(_make_cfg()) is None
