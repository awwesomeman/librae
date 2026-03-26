from __future__ import annotations

from scripts.run_trendpullback_backtest import build_output
from quant_lab.monitoring.influx_writer import points_from_backtest


def test_points_from_backtest_contains_required_measurements() -> None:
    output = build_output("test-run-id")
    points = points_from_backtest(output)
    names = [p._name for p in points]
    assert "strategy_signals" in names
    assert "strategy_performance" in names
    assert "perf_equity_curve" in names
    assert "trade_blotter" in names
    assert "trade_distribution" in names
