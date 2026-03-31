"""Tests for backtest output schema and persistence."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from librae.backtest.schema import (
    SCHEMA_VERSION,
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
    TradeRecord,
)
from librae.backtest.persistence import save_output, load_output

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)
START = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 3, 5, 23, 59, 0, tzinfo=timezone.utc)


def _make_run_metadata(**kwargs) -> RunMetadata:
    defaults = dict(
        run_id="demobreakout_v1-mxfr1-20260306t120000-abcd1234",
        strategy="DemoBreakout_v1",
        symbol="MXFR1",
        timeframe="H1",
        start_ts=START,
        end_ts=END,
        run_ts=NOW,
    )
    defaults.update(kwargs)
    return RunMetadata(**defaults)


def _make_equity_curve() -> list[EquityCurvePoint]:
    return [
        EquityCurvePoint(
            ts=datetime(2026, 3, 1, 10, 0, 0, tzinfo=timezone.utc),
            equity=1_000_000.0,
            ret_1d=0.0,
            drawdown=0.0,
            benchmark_equity=1_000_000.0,
            benchmark_ret_1d=0.0,
        ),
        EquityCurvePoint(
            ts=datetime(2026, 3, 2, 10, 0, 0, tzinfo=timezone.utc),
            equity=1_005_000.0,
            ret_1d=0.005,
            drawdown=0.0,
            benchmark_equity=1_001_000.0,
            benchmark_ret_1d=0.001,
        ),
    ]


def _make_trades() -> list[TradeRecord]:
    return [
        TradeRecord(
            trade_id="t001",
            entry_ts=datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc),
            exit_ts=datetime(2026, 3, 1, 15, 0, 0, tzinfo=timezone.utc),
            symbol="MXFR1",
            side="buy",
            entry_price=21000.0,
            exit_price=21200.0,
            quantity=1.0,
            gross_pnl=200.0,
            net_pnl=180.0,
            commission=20.0,
            slippage=0.0,
            holding_bars=6,
        )
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


def test_schema_version_constant() -> None:
    assert SCHEMA_VERSION == "1.0.0"


def test_run_metadata_defaults() -> None:
    meta = _make_run_metadata()
    assert meta.schema_version == "1.0.0"
    assert meta.schema_version  # basic sanity check


def test_backtest_output_validate_passes() -> None:
    output = BacktestOutput(
        run_metadata=_make_run_metadata(),
        equity_curve=_make_equity_curve(),
        trades=_make_trades(),
        metrics=_make_metrics(),
    )
    output.validate()  # should not raise


def test_backtest_output_validate_empty_run_id_raises() -> None:
    meta = _make_run_metadata(run_id="")
    output = BacktestOutput(
        run_metadata=meta,
        equity_curve=[],
        trades=[],
        metrics=_make_metrics(),
    )
    with pytest.raises(ValueError, match="run_id"):
        output.validate()


def test_backtest_output_validate_empty_strategy_raises() -> None:
    meta = _make_run_metadata(strategy="")
    output = BacktestOutput(
        run_metadata=meta,
        equity_curve=[],
        trades=[],
        metrics=_make_metrics(),
    )
    with pytest.raises(ValueError, match="strategy"):
        output.validate()


def test_trade_record_cost_fields_optional() -> None:
    tr = TradeRecord(
        trade_id="t002",
        entry_ts=NOW,
        exit_ts=NOW,
        symbol="BTCUSDT",
        side="sell",
        entry_price=50000.0,
        exit_price=49000.0,
        quantity=0.1,
        gross_pnl=-100.0,
        net_pnl=-100.0,
    )
    assert tr.commission is None
    assert tr.slippage is None
    assert tr.holding_bars is None


def test_strategy_metrics_cost_fields_optional() -> None:
    m = StrategyMetrics(total_return=0.05)
    assert m.total_commission is None
    assert m.total_slippage is None


def test_equity_curve_point_benchmark_optional() -> None:
    pt = EquityCurvePoint(
        ts=NOW,
        equity=1_000_000.0,
        ret_1d=0.01,
        drawdown=-0.005,
    )
    assert pt.benchmark_equity is None
    assert pt.benchmark_ret_1d is None


# ---------------------------------------------------------------------------
# Persistence tests
# ---------------------------------------------------------------------------


def _make_full_output(**kwargs) -> BacktestOutput:
    return BacktestOutput(
        run_metadata=_make_run_metadata(**kwargs),
        equity_curve=_make_equity_curve(),
        trades=_make_trades(),
        metrics=_make_metrics(),
    )


def test_save_and_load_roundtrip() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir)
        assert "json" in paths
        assert "csv" in paths
        assert paths["json"].exists()
        assert paths["csv"].exists()

        loaded = load_output(paths["json"])
        assert loaded.run_metadata.run_id == output.run_metadata.run_id
        assert loaded.run_metadata.strategy == output.run_metadata.strategy
        assert loaded.metrics.total_return == output.metrics.total_return
        assert len(loaded.equity_curve) == len(output.equity_curve)
        assert len(loaded.trades) == len(output.trades)


def test_save_json_has_required_top_level_keys() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir)
        data = json.loads(paths["json"].read_text())
    assert set(data.keys()) == {"schema_version", "run_metadata", "equity_curve", "trades", "metrics"}


def test_save_equity_csv_has_correct_columns() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir)
        lines = paths["csv"].read_text().splitlines()
    header = lines[0].split(",")
    assert "ts" in header
    assert "equity" in header
    assert "ret_1d" in header
    assert "drawdown" in header


def test_save_no_csv_when_flag_false() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir, save_equity_csv=False)
    assert "csv" not in paths


def test_load_roundtrip_preserves_cost_fields() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir)
        loaded = load_output(paths["json"])
    t = loaded.trades[0]
    assert t.commission == 20.0
    assert t.slippage == 0.0
    assert t.holding_bars == 6


def test_equity_curve_ts_roundtrip_iso() -> None:
    output = _make_full_output()
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = save_output(output, tmpdir)
        loaded = load_output(paths["json"])
    assert loaded.equity_curve[0].ts == output.equity_curve[0].ts
