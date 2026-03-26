"""Tests for the pluggable metrics module.

Unit tests: each built-in metric in isolation.
Integration: compute_all on a full BacktestOutput with trades + equity curve.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quant_lab.backtest.metrics import (
    MetricResult,
    compute_all,
    compute_one,
    get_registry,
    register_metric,
)
from quant_lab.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)
START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2024, 2, 1, 0, 0, 0, tzinfo=timezone.utc)


def _meta(**kw) -> RunMetadata:
    defaults = dict(
        run_id="test-btcusdt-20240101t000000-abcd1234",
        strategy="test_strategy",
        symbol="BTCUSDT",
        timeframe="H1",
        start_ts=START,
        end_ts=END,
        run_ts=NOW,
        data_source="synthetic",
        data_version="1",
    )
    defaults.update(kw)
    return RunMetadata(**defaults)


def _trade(entry: float, exit_: float, gross_pnl: float | None = None, side: str = "buy") -> TradeRecord:
    pnl = gross_pnl if gross_pnl is not None else (exit_ - entry)
    return TradeRecord(
        trade_id="t",
        entry_ts=START,
        exit_ts=END,
        symbol="BTCUSDT",
        side=side,
        entry_price=entry,
        exit_price=exit_,
        quantity=1.0,
        price_unit="USDT",
        quantity_unit="BTC",
        gross_pnl=pnl,
        net_pnl=pnl,
        pnl_unit="USDT",
    )


def _output(trades: list[TradeRecord] | None = None, equity_curve=None) -> BacktestOutput:
    return BacktestOutput(
        run_metadata=_meta(),
        equity_curve=equity_curve or [],
        trades=trades or [],
        metrics=StrategyMetrics(total_return=0.0),
    )


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_builtin_metrics_registered(self):
        registry = get_registry()
        expected = {
            "sharpe", "sortino", "max_drawdown", "calmar",
            "profit_factor", "win_rate", "avg_pnl_points",
            "total_return", "annual_return", "payoff_ratio", "expectancy",
        }
        assert expected.issubset(set(registry.keys()))

    def test_register_custom_metric(self):
        @register_metric("test_custom_metric", "test_unit")
        def _custom(output: BacktestOutput) -> float:
            return 42.0

        registry = get_registry()
        assert "test_custom_metric" in registry

        result = compute_one("test_custom_metric", _output())
        assert result.value == 42.0
        assert result.unit == "test_unit"

    def test_compute_one_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown metric"):
            compute_one("nonexistent_metric", _output())


# ---------------------------------------------------------------------------
# Unit tests: individual metrics
# ---------------------------------------------------------------------------


class TestSharpe:
    def test_no_trades(self):
        result = compute_one("sharpe", _output([]))
        assert result.value == 0.0
        assert result.unit == "score"

    def test_single_trade(self):
        result = compute_one("sharpe", _output([_trade(100, 110)]))
        assert result.value == 0.0  # < 2 trades

    def test_positive_sharpe(self):
        trades = [
            _trade(100, 110),
            _trade(100, 108),
            _trade(100, 112),
            _trade(100, 105),
        ]
        result = compute_one("sharpe", _output(trades))
        assert result.value > 0

    def test_mixed_trades(self):
        trades = [
            _trade(100, 110),  # +10%
            _trade(100, 85),   # -15%
            _trade(100, 105),  # +5%
            _trade(100, 90),   # -10%
        ]
        result = compute_one("sharpe", _output(trades))
        assert result.value < 0  # net negative expected

    def test_side_aware_short_trades(self):
        """Short trade: entry 120, exit 100 should be +16.7% return."""
        trades = [
            _trade(100, 110, side="buy"),    # +10%
            _trade(120, 100, side="sell"),    # sell: -(100-120)/120 = +16.7%
        ]
        result = compute_one("sharpe", _output(trades))
        assert result.value > 0  # both profitable


class TestSortino:
    def test_no_trades(self):
        result = compute_one("sortino", _output([]))
        assert result.value == 0.0

    def test_all_winners_infinite(self):
        trades = [_trade(100, 110), _trade(100, 120)]
        result = compute_one("sortino", _output(trades))
        assert result.value == float("inf")

    def test_mixed(self):
        trades = [
            _trade(100, 115, gross_pnl=15),
            _trade(100, 90, gross_pnl=-10),
            _trade(100, 108, gross_pnl=8),
        ]
        result = compute_one("sortino", _output(trades))
        assert isinstance(result.value, float)


class TestMaxDrawdown:
    def test_no_data(self):
        result = compute_one("max_drawdown", _output([]))
        assert result.value == 0.0

    def test_from_trades(self):
        trades = [
            _trade(100, 110),  # +10%
            _trade(100, 80),   # -20%
            _trade(100, 105),  # +5%
        ]
        result = compute_one("max_drawdown", _output(trades))
        assert result.value > 0
        assert result.unit == "ratio"

    def test_from_equity_curve(self):
        points = [
            EquityCurvePoint(ts=START, equity=100, equity_unit="USDT", ret_1d=0.0, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=120, equity_unit="USDT", ret_1d=0.2, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=90, equity_unit="USDT", ret_1d=-0.25, drawdown=-0.25),
            EquityCurvePoint(ts=START, equity=110, equity_unit="USDT", ret_1d=0.22, drawdown=0.0),
        ]
        result = compute_one("max_drawdown", _output([], points))
        assert result.value == pytest.approx(0.25, abs=1e-9)

    def test_monotonic_increase_zero_dd(self):
        points = [
            EquityCurvePoint(ts=START, equity=100, equity_unit="USDT", ret_1d=0.0, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=110, equity_unit="USDT", ret_1d=0.1, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=120, equity_unit="USDT", ret_1d=0.09, drawdown=0.0),
        ]
        result = compute_one("max_drawdown", _output([], points))
        assert result.value == 0.0


class TestCalmar:
    def test_no_data(self):
        result = compute_one("calmar", _output([]))
        assert result.value == 0.0

    def test_zero_drawdown_positive_return(self):
        trades = [_trade(100, 110), _trade(100, 120)]
        result = compute_one("calmar", _output(trades))
        assert result.value == float("inf")


class TestProfitFactor:
    def test_no_trades(self):
        result = compute_one("profit_factor", _output([]))
        assert result.value == 0.0

    def test_all_winners(self):
        trades = [_trade(100, 110), _trade(100, 120)]
        result = compute_one("profit_factor", _output(trades))
        assert result.value == float("inf")

    def test_mixed(self):
        trades = [
            _trade(100, 120, gross_pnl=20),   # +20
            _trade(100, 90, gross_pnl=-10),    # -10
        ]
        result = compute_one("profit_factor", _output(trades))
        assert result.value == pytest.approx(2.0, abs=1e-9)

    def test_all_losers(self):
        trades = [_trade(100, 90, gross_pnl=-10), _trade(100, 85, gross_pnl=-15)]
        result = compute_one("profit_factor", _output(trades))
        assert result.value == 0.0


class TestWinRate:
    def test_no_trades(self):
        result = compute_one("win_rate", _output([]))
        assert result.value == 0.0

    def test_all_winners(self):
        trades = [_trade(100, 110, gross_pnl=10), _trade(100, 120, gross_pnl=20)]
        result = compute_one("win_rate", _output(trades))
        assert result.value == 1.0

    def test_half_and_half(self):
        trades = [
            _trade(100, 110, gross_pnl=10),
            _trade(100, 90, gross_pnl=-10),
        ]
        result = compute_one("win_rate", _output(trades))
        assert result.value == pytest.approx(0.5)


class TestAvgPnlPoints:
    def test_no_trades(self):
        result = compute_one("avg_pnl_points", _output([]))
        assert result.value == 0.0

    def test_calculation(self):
        trades = [
            _trade(100, 120),  # +20 points
            _trade(100, 90),   # -10 points
            _trade(100, 115),  # +15 points
        ]
        result = compute_one("avg_pnl_points", _output(trades))
        expected = (20 + (-10) + 15) / 3
        assert result.value == pytest.approx(expected, abs=1e-9)
        assert result.unit == "points"

    def test_short_side_awareness(self):
        """Short trades: entry > exit is a profit in points."""
        short_win = TradeRecord(
            trade_id="t", entry_ts=START, exit_ts=END, symbol="BTCUSDT",
            side="sell", entry_price=120, exit_price=100, quantity=1.0,
            price_unit="USDT", quantity_unit="BTC",
            gross_pnl=20, net_pnl=20, pnl_unit="USDT",
        )
        short_loss = TradeRecord(
            trade_id="t", entry_ts=START, exit_ts=END, symbol="BTCUSDT",
            side="sell", entry_price=100, exit_price=115, quantity=1.0,
            price_unit="USDT", quantity_unit="BTC",
            gross_pnl=-15, net_pnl=-15, pnl_unit="USDT",
        )
        result = compute_one("avg_pnl_points", _output([short_win, short_loss]))
        # short_win: -(100-120) = +20, short_loss: -(115-100) = -15
        expected = (20 + (-15)) / 2
        assert result.value == pytest.approx(expected, abs=1e-9)


class TestTotalReturn:
    def test_no_data(self):
        result = compute_one("total_return", _output([]))
        assert result.value == 0.0

    def test_from_trades(self):
        trades = [_trade(100, 110), _trade(100, 120)]  # +10%, +20%
        result = compute_one("total_return", _output(trades))
        expected = (1.10 * 1.20) - 1.0
        assert result.value == pytest.approx(expected, abs=1e-9)


class TestAnnualReturn:
    def test_no_data(self):
        result = compute_one("annual_return", _output([]))
        assert result.value == 0.0


class TestPayoffRatio:
    def test_no_trades(self):
        result = compute_one("payoff_ratio", _output([]))
        assert result.value == 0.0

    def test_all_winners_returns_zero(self):
        trades = [_trade(100, 110, gross_pnl=10)]
        result = compute_one("payoff_ratio", _output(trades))
        assert result.value == 0.0  # no losses to compare

    def test_mixed(self):
        trades = [
            _trade(100, 120, gross_pnl=20),
            _trade(100, 90, gross_pnl=-10),
        ]
        result = compute_one("payoff_ratio", _output(trades))
        assert result.value == pytest.approx(2.0, abs=1e-9)


class TestExpectancy:
    def test_no_trades(self):
        result = compute_one("expectancy", _output([]))
        assert result.value == 0.0

    def test_positive_expectancy(self):
        trades = [
            _trade(100, 130, gross_pnl=30),
            _trade(100, 90, gross_pnl=-10),
            _trade(100, 120, gross_pnl=20),
        ]
        result = compute_one("expectancy", _output(trades))
        # avg_win=25, win_rate=2/3, avg_loss=10, loss_rate=1/3
        expected = 25 * (2 / 3) - 10 * (1 / 3)
        assert result.value == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# Integration: compute_all
# ---------------------------------------------------------------------------


class TestComputeAllIntegration:
    @pytest.fixture
    def full_output(self):
        trades = [
            _trade(100, 115, gross_pnl=15),   # +15%
            _trade(100, 90, gross_pnl=-10),    # -10%
            _trade(100, 108, gross_pnl=8),     # +8%
            _trade(100, 95, gross_pnl=-5),     # -5%
            _trade(100, 120, gross_pnl=20),    # +20%
        ]
        equity_points = [
            EquityCurvePoint(ts=START, equity=1000, equity_unit="USDT", ret_1d=0.0, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=1150, equity_unit="USDT", ret_1d=0.15, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=1035, equity_unit="USDT", ret_1d=-0.10, drawdown=-0.10),
            EquityCurvePoint(ts=START, equity=1117.8, equity_unit="USDT", ret_1d=0.08, drawdown=0.0),
            EquityCurvePoint(ts=START, equity=1061.91, equity_unit="USDT", ret_1d=-0.05, drawdown=-0.05),
            EquityCurvePoint(ts=START, equity=1274.29, equity_unit="USDT", ret_1d=0.20, drawdown=0.0),
        ]
        return BacktestOutput(
            run_metadata=_meta(),
            equity_curve=equity_points,
            trades=trades,
            metrics=StrategyMetrics(total_return=0.2743),
        )

    def test_compute_all_returns_all_builtins(self, full_output):
        results = compute_all(full_output)
        expected_names = {
            "sharpe", "sortino", "max_drawdown", "calmar",
            "profit_factor", "win_rate", "avg_pnl_points",
            "total_return", "annual_return", "payoff_ratio", "expectancy",
        }
        assert expected_names.issubset(set(results.keys()))

    def test_all_results_are_metric_result(self, full_output):
        results = compute_all(full_output)
        for name, result in results.items():
            assert isinstance(result, MetricResult), f"{name} is not MetricResult"
            assert isinstance(result.value, float), f"{name}.value is not float"

    def test_integration_win_rate(self, full_output):
        results = compute_all(full_output)
        assert results["win_rate"].value == pytest.approx(3 / 5)

    def test_integration_profit_factor(self, full_output):
        results = compute_all(full_output)
        pf = results["profit_factor"].value
        assert pf == pytest.approx((15 + 8 + 20) / (10 + 5), abs=1e-9)

    def test_integration_avg_pnl_points(self, full_output):
        results = compute_all(full_output)
        expected = (15 + (-10) + 8 + (-5) + 20) / 5
        assert results["avg_pnl_points"].value == pytest.approx(expected, abs=1e-9)

    def test_integration_max_drawdown_from_equity(self, full_output):
        results = compute_all(full_output)
        mdd = results["max_drawdown"].value
        assert mdd == pytest.approx(0.1, abs=1e-2)

    def test_integration_sharpe_positive(self, full_output):
        results = compute_all(full_output)
        assert results["sharpe"].value > 0

    def test_integration_sortino_positive(self, full_output):
        results = compute_all(full_output)
        assert results["sortino"].value > 0

    def test_integration_expectancy(self, full_output):
        results = compute_all(full_output)
        assert results["expectancy"].value > 0  # net positive trades

    def test_error_isolation(self):
        """A broken custom metric should not break compute_all."""
        @register_metric("_test_broken_metric", "unit")
        def _broken(output: BacktestOutput) -> float:
            raise RuntimeError("intentional error")

        trades = [_trade(100, 110, gross_pnl=10)]
        results = compute_all(_output(trades))
        assert "_test_broken_metric" not in results
        assert "win_rate" in results
