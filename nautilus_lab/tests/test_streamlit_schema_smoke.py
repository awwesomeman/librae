import pandas as pd
import pytest

from app.streamlit_performance import (
    SchemaValidationError,
    _require_columns,
    _require_perf_fields,
    build_general_metrics_table,
    normalize_position,
)


def test_require_columns_raises_on_missing_columns() -> None:
    df = pd.DataFrame({"_time": ["2026-01-01T00:00:00Z"], "price": [1.0]})
    with pytest.raises(SchemaValidationError, match="missing required columns"):
        _require_columns(df, {"_time", "price", "side", "run_id"}, "strategy_signals")


def test_require_perf_fields_raises_on_missing_fields() -> None:
    perf = pd.DataFrame(
        {
            "_field": ["total_return", "max_drawdown", "trades"],
            "_value": [0.1, -0.2, 50],
        }
    )
    with pytest.raises(SchemaValidationError, match="strategy_performance missing required fields"):
        _require_perf_fields(perf)


def test_active_period_metrics_do_not_silent_fallback_to_full_period() -> None:
    perf = pd.DataFrame(
        {
            "_field": ["total_return", "max_drawdown", "trades", "profit_factor", "win_rate", "avg_trade_return", "exposure_ratio", "bh_total_return"],
            "_value": [0.08, -0.03, 10, 1.2, 0.55, 0.01, 0.3, 0.04],
        }
    )
    table = build_general_metrics_table(perf)
    row = table.loc[table["Metric"] == "Total Return (Active Period)", "Strategy"].iloc[0]
    assert row == "-"


def test_unknown_position_is_not_forced_to_buy() -> None:
    assert normalize_position("flat") == "unknown"
