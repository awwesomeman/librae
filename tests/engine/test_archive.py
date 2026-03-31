"""Tests for Parquet archiving (backtest.archive).

Skills: python, quant
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from librae.backtest.persistence import archive_backtest_parquet
from librae.backtest.schema import (
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)


def _make_sample_output(run_id: str = "test_strat-btcusdt-20260326t000000-abcd1234") -> BacktestOutput:
    """Create a minimal but valid BacktestOutput for testing."""
    now = datetime(2026, 3, 26, tzinfo=timezone.utc)
    meta = RunMetadata(
        run_id=run_id,
        strategy="test_strat",
        symbol="BTCUSDT",
        timeframe="H1",
        start_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_ts=datetime(2026, 3, 1, tzinfo=timezone.utc),
        run_ts=now,
    )
    equity = [
        EquityCurvePoint(
            ts=datetime(2026, 1, 1, h, tzinfo=timezone.utc),
            equity=1.0 + h * 0.001,
            ret_1d=0.001,
            drawdown=0.0,
        )
        for h in range(5)
    ]
    trades = [
        TradeRecord(
            trade_id=f"{run_id}-t0000",
            entry_ts=datetime(2026, 1, 2, tzinfo=timezone.utc),
            exit_ts=datetime(2026, 1, 3, tzinfo=timezone.utc),
            symbol="BTCUSDT",
            side="buy",
            entry_price=40000.0,
            exit_price=41000.0,
            quantity=0.1,
            gross_pnl=100.0,
            net_pnl=100.0,
            holding_bars=24,
        ),
    ]
    metrics = StrategyMetrics(total_return=0.025, trades=1, sharpe=1.5, max_drawdown=0.01)
    return BacktestOutput(run_metadata=meta, equity_curve=equity, trades=trades, metrics=metrics)


class TestArchiveParquet:
    """Verify Parquet archive round-trip read/write."""

    def test_archive_creates_files_and_roundtrip(self):
        output = _make_sample_output()
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = archive_backtest_parquet(output, tmpdir)

            # Files exist
            assert paths["equity_curve"].exists()
            assert paths["trades"].exists()

            # Correct directory structure
            run_id = output.run_metadata.run_id
            assert f"archive/{run_id}" in str(paths["equity_curve"])

            # Read back and validate equity curve
            eq_df = pd.read_parquet(paths["equity_curve"])
            assert len(eq_df) == 5
            assert "ts" in eq_df.columns
            assert "equity" in eq_df.columns
            assert eq_df["equity"].iloc[0] == pytest.approx(1.0)

            # Read back and validate trades
            tr_df = pd.read_parquet(paths["trades"])
            assert len(tr_df) == 1
            assert tr_df["entry_price"].iloc[0] == pytest.approx(40000.0)
            assert tr_df["net_pnl"].iloc[0] == pytest.approx(100.0)

    def test_archive_empty_equity_and_trades(self):
        output = _make_sample_output()
        # Replace with empty sequences
        output = BacktestOutput(
            run_metadata=output.run_metadata,
            equity_curve=[],
            trades=[],
            metrics=StrategyMetrics(total_return=0.0, trades=0),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = archive_backtest_parquet(output, tmpdir)
            assert paths["equity_curve"].exists()
            assert paths["trades"].exists()
            eq_df = pd.read_parquet(paths["equity_curve"])
            tr_df = pd.read_parquet(paths["trades"])
            assert len(eq_df) == 0
            assert len(tr_df) == 0
