import pandas as pd
import pytest

from app.streamlit_performance import (
    SchemaValidationError,
    _require_columns,
    _require_perf_fields,
    build_general_metrics_table,
    normalize_position,
    validate_strategy_context_or_raise,
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


def test_general_metrics_table_uses_canonical_fields() -> None:
    perf = pd.DataFrame(
        {
            "_field": ["total_return", "annual_return", "sharpe", "max_drawdown", "win_rate", "trades"],
            "_value": [0.08, 0.12, 1.4, -0.03, 0.55, 10],
        }
    )
    curve = pd.DataFrame({"benchmark_equity": [1.0, 1.05]})
    table = build_general_metrics_table(perf, curve)
    row = table.loc[table["Metric"] == "Total Return", "Strategy"].iloc[0]
    assert row == "8.00%"


def test_unknown_position_is_not_forced_to_buy() -> None:
    assert normalize_position("flat") == "unknown"


def test_strategy_context_requires_canonical_keys() -> None:
    bad_meta = {
        "benchmark": "TWSE",
        "data_source": "seed",
        "data_version": "v1",
        "last_updated_utc": "2026-03-06T00:00:00Z",
        "summary": {"full_sample_period": "2026-01-01~2026-01-31"},
    }
    with pytest.raises(SchemaValidationError, match="missing required keys"):
        validate_strategy_context_or_raise(bad_meta, "DemoStrategy")
