"""Tests for CLI config merge logic (parse_with_config)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from librae.core.run_config import AccountConfig, RunConfig

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
        assert ns.reconciliation_interval_seconds is None
        assert ns.market_data_workers is None

    def test_yaml_scalars_become_argparse_defaults(self, config_yaml):
        cfg = config_yaml("""\
            mode: sim
            poll-seconds: 30
            reconciliation-interval-seconds: 60
            market-data-workers: 2
        """)
        p = base_parser("test")
        ns = parse_with_config(p, config_path=cfg)
        assert ns.mode == "sim"
        assert ns.poll_seconds == 30
        assert ns.reconciliation_interval_seconds == 60
        assert ns.market_data_workers == 2

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
    def test_execution_policy_defaults_and_explicit_unlimited(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                """
            )
        )

        capped = build_config("test_strat", str(tmp_path / "run.py"))
        assert capped.execution.default_fill_price == "open"
        assert capped.execution.max_bar_volume_participation_rate == 0.1

        config_path.write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  execution:
                    default_fill_price: close
                    max_bar_volume_participation_rate: null
                    live_order_timeout_seconds: 120
                """
            )
        )
        unlimited = build_config("test_strat", str(tmp_path / "run.py"))
        assert unlimited.execution.default_fill_price == "close"
        assert unlimited.execution.max_bar_volume_participation_rate is None
        assert unlimited.execution.live_order_timeout_seconds == 120

    def test_unknown_execution_setting_is_rejected(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  execution:
                    volume_limit: 0.1
                """
            )
        )

        with pytest.raises(ValueError, match=r"unknown strategy\.execution"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_legacy_symbol_override_name_is_rejected(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  symbol_overrides:
                    MU:
                      multiplier: 1.0
                """
            )
        )

        with pytest.raises(ValueError, match="symbol_cost_overrides"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_legacy_annual_period_name_is_rejected(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  perf:
                    annual_periods: 252
                """
            )
        )

        with pytest.raises(ValueError, match=r"unknown strategy\.perf"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_adv_execution_settings_are_typed(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: D1
                  execution:
                    adv_lookback_sessions: 20
                    max_adv_participation_rate: 0.01
                """
            )
        )

        config = build_config("test_strat", str(tmp_path / "run.py"))

        assert config.execution.adv_lookback_sessions == 20
        assert config.execution.max_adv_participation_rate == pytest.approx(0.01)

    def test_risk_policy_is_typed_and_unknown_keys_are_rejected(self, tmp_path):
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  risk:
                    max_position_weight: 0.25
                    max_drawdown_rate: 0.20
                    max_order_notional: 25000
                    max_limit_price_deviation_rate: 0.10
                """
            )
        )

        config = build_config("test_strat", str(tmp_path / "run.py"))
        assert config.risk.max_position_weight == 0.25
        assert config.risk.max_drawdown_rate == 0.20
        assert config.risk.max_order_notional == 25_000
        assert config.risk.max_limit_price_deviation_rate == 0.10

        config_path.write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  risk:
                    drawdown_pct: 20
                """
            )
        )
        with pytest.raises(ValueError, match=r"unknown strategy\.risk"):
            build_config("test_strat", str(tmp_path / "run.py"))

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
                  symbol_cost_overrides:
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

        assert cfg.symbol_cost_overrides == {"AAPL": {"multiplier": 1.0}}
        assert cfg.broker == "ibkr"
        assert cfg.periods_per_year == 252
        assert cfg.instrument_overrides == {
            "AAPL": {
                "data_adapter": "ibkr",
                "currency": "USD",
                "security_type": "STK",
            }
        }

    def test_mixed_data_sources_require_explicit_periods_per_year(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbols: [MU, BTCUSDT]
                  timeframe: 1d
                  accounts:
                    ibkr:
                      currency: USD
                      initial_cash: 100000
                    binance:
                      currency: USDT
                      initial_cash: 100000
                  instrument_overrides:
                    MU:
                      account_id: ibkr
                    BTCUSDT:
                      account_id: binance
                """
            )
        )

        with pytest.raises(ValueError, match="periods_per_year"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_intraday_annualization_requires_explicit_periods_per_year(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: BTCUSDT
                  timeframe: H1
                """
            )
        )

        with pytest.raises(ValueError, match="periods_per_year"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_unknown_data_source_requires_explicit_periods_per_year(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: TEST
                  timeframe: 1d
                  market: test
                  data_source: custom
                  accounts:
                    default:
                      currency: USD
                      initial_cash: 100000
                  symbol_cost_overrides:
                    TEST:
                      multiplier: 1.0
                """
            )
        )

        with pytest.raises(ValueError, match="periods_per_year"):
            build_config("test_strat", str(tmp_path / "run.py"))

    def test_unknown_data_source_without_annualization_needs_no_calendar(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: TEST
                  timeframe: 1d
                  market: test
                  data_source: custom
                  accounts:
                    default:
                      currency: USD
                      initial_cash: 100000
                  perf:
                    annualize: false
                """
            )
        )

        cfg = build_config("test_strat", str(tmp_path / "run.py"))

        assert cfg.annualize is False

    def test_periods_per_year_must_be_positive(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            textwrap.dedent(
                """\
                strategy:
                  symbol: MU
                  timeframe: 1d
                  perf:
                    periods_per_year: 0
                """
            )
        )

        with pytest.raises(ValueError, match="periods_per_year must be a positive integer"):
            build_config("test_strat", str(tmp_path / "run.py"))


def _make_cfg(**overrides) -> RunConfig:
    defaults = dict(
        strategy_name="test_strat",
        symbols=["MU"],
        timeframe="1d",
        market="us_equity",
        data_source="local",
        accounts={"default": AccountConfig(currency="USD", initial_cash=100_000.0)},
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

    def test_changed_perf_params_refresh_every_configured_account(self):
        config = _make_cfg(
            accounts={
                "alpha": AccountConfig(currency="USD", initial_cash=100_000.0),
                "beta": AccountConfig(currency="USD", initial_cash=50_000.0),
            }
        )
        existing = {
            "run_id": "existing-run",
            "perf_params": {"annualize": not config.annualize},
        }

        with (
            patch(
                "db.timescale_reader.get_run_by_config_hash",
                return_value=existing,
            ),
            patch("db.timescale_writer.refresh_performance") as refresh,
            patch("db.timescale_writer._update_perf_params") as update_params,
        ):
            assert check_existing_run(config) == "existing-run"

        assert [call.kwargs["account_id"] for call in refresh.call_args_list] == [
            "alpha",
            "beta",
        ]
        assert all(call.kwargs["config"] is config for call in refresh.call_args_list)
        update_params.assert_called_once_with("existing-run", config.perf_params)
