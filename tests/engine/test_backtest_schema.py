"""Tests for backtest output schema."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from librae.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
)

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 3, 5, 23, 59, 0, tzinfo=timezone.utc)


def _make_run_metadata(**kwargs) -> RunMetadata:
    defaults = dict(
        run_id="demobreakout_v1-mxfr1-20260306t120000-abcd1234",
        strategy="DemoBreakout_v1",
        symbol="MXFR1",
        timeframe="H1",
        data_source="binance_spot",
        started_at=START,
        ended_at=END,
        run_at=NOW,
    )
    defaults.update(kwargs)
    return RunMetadata(**defaults)


def _make_equity_curve() -> list[EquityCurvePoint]:
    return [
        EquityCurvePoint(
            ts=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            equity=1_000_000.0,
            period_return=0.0,
            drawdown=0.0,
            benchmark_equity=1_000_000.0,
            benchmark_period_return=0.0,
        ),
        EquityCurvePoint(
            ts=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            equity=1_005_000.0,
            period_return=0.005,
            drawdown=0.0,
            benchmark_equity=1_001_000.0,
            benchmark_period_return=0.001,
        ),
    ]


def _make_metrics() -> StrategyMetrics:
    return StrategyMetrics(
        total_return=0.05,
        annual_return=0.30,
        sharpe=1.2,
        max_drawdown=-0.03,
        win_rate=0.6,
        profit_factor=1.8,
        avg_trade_return=0.01,
        trades=10,
        exposure_ratio=0.4,
    )


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_run_metadata_defaults() -> None:
    meta = _make_run_metadata()
    assert meta.mode == "backtest"
    assert meta.data_source == "binance_spot"


def test_backtest_output_validate_passes() -> None:
    output = BacktestOutput(
        run_metadata=_make_run_metadata(),
        equity_curve=_make_equity_curve(),
        order_events=(),
        metrics=_make_metrics(),
    )
    output.validate()  # should not raise


def test_backtest_output_validate_empty_run_id_raises() -> None:
    meta = _make_run_metadata(run_id="")
    output = BacktestOutput(
        run_metadata=meta,
        equity_curve=[],
        order_events=(),
        metrics=_make_metrics(),
    )
    with pytest.raises(ValueError, match="run_id"):
        output.validate()


def test_backtest_output_validate_empty_strategy_raises() -> None:
    meta = _make_run_metadata(strategy="")
    output = BacktestOutput(
        run_metadata=meta,
        equity_curve=[],
        order_events=(),
        metrics=_make_metrics(),
    )
    with pytest.raises(ValueError, match="strategy"):
        output.validate()


def test_strategy_metrics_cost_fields_optional() -> None:
    m = StrategyMetrics(total_return=0.05)
    assert m.total_commission is None
    assert m.total_slippage is None


def test_equity_curve_point_benchmark_optional() -> None:
    pt = EquityCurvePoint(
        ts=NOW,
        equity=1_000_000.0,
        period_return=0.01,
        drawdown=-0.005,
    )
    assert pt.benchmark_equity is None
    assert pt.benchmark_period_return is None


