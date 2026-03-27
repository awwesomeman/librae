import pandas as pd
import pytest

from app.streamlit_performance import (
    SchemaValidationError,
    _require_columns,
    _require_perf_fields,
    build_general_metrics_table,
    build_order_details,
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


def _make_blotter(**extra_cols):
    """Helper: minimal blotter DataFrame for build_order_details tests."""
    base = {
        "_time": pd.to_datetime(["2026-03-04T08:00:00+00:00"]),
        "side": ["buy"],
        "entry_price": [18000.0],
        "exit_price": [18200.0],
        "quantity": [1.0],
        "holding_bars": [4],
        "net_pnl": [150.0],
    }
    base.update(extra_cols)
    return pd.DataFrame(base)


def test_build_order_details_has_entry_time_column() -> None:
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"])
    result = build_order_details(blotter)
    assert "Entry Time" in result.columns


def test_build_order_details_entry_time_column_order() -> None:
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"])
    result = build_order_details(blotter)
    cols = list(result.columns)
    assert cols[0] == "Entry Time"
    assert cols[1] == "Exit Time"
    assert cols[2] == "Position"


def test_build_order_details_entry_time_empty_string_shows_dash() -> None:
    blotter = _make_blotter(entry_time=[""])
    result = build_order_details(blotter)
    assert result["Entry Time"].iloc[0] == "—"


def test_build_order_details_entry_time_missing_column_shows_dash() -> None:
    blotter = _make_blotter()  # no entry_time column
    result = build_order_details(blotter)
    assert "Entry Time" in result.columns
    assert result["Entry Time"].iloc[0] == "—"


def test_build_order_details_empty_blotter_has_entry_time_column() -> None:
    result = build_order_details(pd.DataFrame())
    assert "Entry Time" in result.columns


def test_build_order_details_no_serial_side_qty_columns() -> None:
    """#, Side, Qty columns should not exist in output."""
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"])
    result = build_order_details(blotter)
    for removed_col in ("#", "Side", "Qty"):
        assert removed_col not in result.columns


def test_build_order_details_position_long() -> None:
    """Long (buy) → +qty formatted to 4 decimals."""
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"])  # side=buy, qty=1.0
    result = build_order_details(blotter)
    assert result["Position"].iloc[0] == "+1.00"


def test_build_order_details_position_short() -> None:
    """Short (sell) → -qty formatted to 4 decimals."""
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"], side=["sell"])
    result = build_order_details(blotter)
    assert result["Position"].iloc[0] == "-1.00"


def test_build_order_details_full_columns() -> None:
    """Output columns must exactly match the expected schema."""
    expected = ["Entry Time", "Exit Time", "Position", "Entry Price", "Exit Price", "Holding Bars", "Gross Return %", "Net Return %"]
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"])
    result = build_order_details(blotter)
    assert list(result.columns) == expected


def test_build_order_details_net_return_pct() -> None:
    """Net Return % = net_pnl / (entry_price * quantity) * 100; must be <= Gross Return % when cost > 0."""
    # entry=18000, exit=18200, qty=1, net_pnl=150 (gross=200 implied by price diff)
    blotter = _make_blotter(entry_time=["2026-03-04 06:00"], net_pnl=[150.0])
    result = build_order_details(blotter)
    assert "Net Return %" in result.columns
    expected_net = round(150.0 / (18000.0 * 1.0) * 100, 2)
    assert result["Net Return %"].iloc[0] == pytest.approx(expected_net)
    # Net Return % <= Gross Return % when there is cost
    assert result["Net Return %"].iloc[0] <= result["Gross Return %"].iloc[0]
