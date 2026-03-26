"""Test influx_writer produces all required measurements from BacktestOutput."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from quant_lab.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)
from quant_lab.monitoring.influx_writer import points_from_backtest


def _build_test_output(run_id: str = "test-run-id") -> BacktestOutput:
    """Build a minimal BacktestOutput for testing influx point generation."""
    now = datetime.now(timezone.utc)
    meta = RunMetadata(
        run_id=run_id,
        strategy="trendpullback_v1_1_h1_l_mxfr1",
        symbol="MXFR1",
        timeframe="H1",
        start_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        end_ts=datetime(2025, 6, 30, tzinfo=timezone.utc),
        run_ts=now,
        data_source="synthetic",
        data_version="synthetic_v1",
    )
    trades = [
        TradeRecord(
            trade_id=f"{run_id}-t0001",
            entry_ts=datetime(2025, 2, 1, 10, 0, tzinfo=timezone.utc),
            exit_ts=datetime(2025, 2, 1, 14, 0, tzinfo=timezone.utc),
            symbol="MXFR1",
            side="buy",
            entry_price=20000.0,
            exit_price=20100.0,
            quantity=1.0,
            price_unit="points",
            quantity_unit="contracts",
            gross_pnl=100.0,
            net_pnl=98.0,
            pnl_unit="points",
            holding_bars=4,
        ),
    ]
    equity_curve = [
        EquityCurvePoint(
            ts=datetime(2025, 2, 1, tzinfo=timezone.utc),
            equity=1.0,
            equity_unit="ratio",
            ret_1d=0.0,
            drawdown=0.0,
            benchmark_equity=1.0,
            benchmark_ret_1d=0.0,
        ),
        EquityCurvePoint(
            ts=datetime(2025, 2, 2, tzinfo=timezone.utc),
            equity=1.005,
            equity_unit="ratio",
            ret_1d=0.005,
            drawdown=0.0,
            benchmark_equity=1.001,
            benchmark_ret_1d=0.001,
        ),
    ]
    metrics = StrategyMetrics(
        total_return=0.005,
        annual_return=0.03,
        sharpe=1.2,
        max_drawdown=0.02,
        win_rate=1.0,
        profit_factor=float("inf"),
        avg_trade_return=0.005,
        trades=1,
    )
    return BacktestOutput(
        run_metadata=meta,
        equity_curve=equity_curve,
        trades=trades,
        metrics=metrics,
    )


def test_points_from_backtest_contains_required_measurements() -> None:
    output = _build_test_output("test-run-id")
    points = points_from_backtest(output)
    names = [p._name for p in points]
    assert "strategy_signals" in names
    assert "strategy_performance" in names
    assert "perf_equity_curve" in names
    assert "trade_blotter" in names
    assert "trade_distribution" in names
