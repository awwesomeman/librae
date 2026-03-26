"""Tests for quant_lab.backtest.adapter — legacy dict -> BacktestOutput."""
from __future__ import annotations

import pytest

from quant_lab.backtest.adapter import generate_run_id, metrics_dict_to_backtest_output
from quant_lab.backtest.schema import BacktestOutput


SAMPLE_METRICS = {
    "trades": 42,
    "win_rate": 0.55,
    "avg_ret": 0.003,
    "pf": 1.8,
    "equity": 1.15,
    "ann_return": 0.12,
    "ann_sharpe": 1.5,
    "ann_vol": 0.08,
    "mdd": 0.07,
}

CONTEXT = {
    "strategy": "TrendPullback_v1.0.0-H1-L-MXFR1",
    "symbol": "MXFR1",
    "timeframe": "H1",
    "start": "2024-01-01",
    "end": "2025-06-30",
    "data_source": "Shioaji",
}


class TestMetricsDictToBacktestOutput:
    def test_returns_backtest_output(self):
        output = metrics_dict_to_backtest_output(SAMPLE_METRICS, **CONTEXT)
        assert isinstance(output, BacktestOutput)

    def test_validate_passes(self):
        output = metrics_dict_to_backtest_output(SAMPLE_METRICS, **CONTEXT)
        output.validate()

    def test_run_metadata_fields(self):
        output = metrics_dict_to_backtest_output(SAMPLE_METRICS, **CONTEXT)
        meta = output.run_metadata
        assert meta.strategy == CONTEXT["strategy"]
        assert meta.symbol == CONTEXT["symbol"]
        assert meta.timeframe == CONTEXT["timeframe"]
        assert meta.run_id
        assert meta.run_ts is not None

    def test_start_end_tz_aware(self):
        output = metrics_dict_to_backtest_output(SAMPLE_METRICS, **CONTEXT)
        meta = output.run_metadata
        assert meta.start_ts.tzinfo is not None
        assert meta.end_ts.tzinfo is not None

    def test_metrics_mapping(self):
        output = metrics_dict_to_backtest_output(SAMPLE_METRICS, **CONTEXT)
        m = output.metrics
        assert m.trades == 42
        assert m.annual_return == pytest.approx(0.12)
        assert m.sharpe == pytest.approx(1.5)
        assert m.max_drawdown == pytest.approx(0.07)
        assert m.total_return == pytest.approx(0.15)

    def test_custom_run_id(self):
        output = metrics_dict_to_backtest_output(
            SAMPLE_METRICS, run_id="my-custom-id", **CONTEXT
        )
        assert output.run_metadata.run_id == "my-custom-id"

    def test_sample_label(self):
        output = metrics_dict_to_backtest_output(
            SAMPLE_METRICS, sample="oos", **CONTEXT
        )
        assert output.run_metadata.sample == "oos"

    def test_zero_trade_conversion(self):
        output = metrics_dict_to_backtest_output({"trades": 0}, **CONTEXT)
        output.validate()
        assert output.metrics.trades == 0
        assert output.metrics.total_return == pytest.approx(0.0)

    def test_none_values_default_to_zero(self):
        metrics = {
            "trades": 3,
            "win_rate": 0.33,
            "avg_ret": -0.01,
            "pf": None,
            "equity": 0.97,
            "ann_return": -0.05,
            "ann_sharpe": None,
            "mdd": 0.03,
        }
        output = metrics_dict_to_backtest_output(metrics, **CONTEXT)
        output.validate()
        assert output.metrics.sharpe == pytest.approx(0.0)
        assert output.metrics.profit_factor == pytest.approx(0.0)


class TestGenerateRunId:
    def test_format(self):
        rid = generate_run_id("MyStrategy", "MXFR1")
        assert rid.startswith("mystrategy-mxfr1-")
        parts = rid.split("-")
        assert len(parts) >= 4

    def test_unique(self):
        ids = {generate_run_id("s", "x") for _ in range(100)}
        assert len(ids) == 100
