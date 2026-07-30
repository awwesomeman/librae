"""Tests for explicit backtest bar normalization."""

from __future__ import annotations

import pandas as pd
import pytest
from librae import normalize_bars


def _bars(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
            "signal": [0.1, 0.2],
        },
        index=index,
    )


def test_normalize_bars_accepts_single_symbol_datetime_index() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="Asia/Taipei"))

    result = normalize_bars(data, symbol="2330")

    assert result.index.names == ["symbol", "datetime"]
    assert str(result.index.get_level_values("datetime").tz) == "UTC"
    assert result["signal"].tolist() == [0.1, 0.2]


def test_normalize_bars_maps_long_form_columns() -> None:
    data = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA"],
            "date": pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"),
            "Open": [100.0, 101.0],
            "High": [101.0, 102.0],
            "Low": [99.0, 100.0],
            "Close": [100.5, 101.5],
            "Volume": [10.0, 11.0],
        }
    )

    result = normalize_bars(
        data,
        column_mapping={
            "ticker": "symbol",
            "date": "datetime",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        },
    )

    assert result.index.names == ["symbol", "datetime"]
    assert result.index.get_level_values("symbol").unique().tolist() == ["AAA"]


def test_normalize_bars_sorts_canonical_data_without_mutating_input() -> None:
    index = pd.MultiIndex.from_arrays(
        [
            ["BBB", "AAA"],
            pd.to_datetime(["2026-01-02", "2026-01-01"], utc=True),
        ],
        names=["symbol", "datetime"],
    )
    data = _bars(index)

    result = normalize_bars(data)

    assert result.index.get_level_values("symbol").tolist() == ["AAA", "BBB"]
    assert data.index.get_level_values("symbol").tolist() == ["BBB", "AAA"]


def test_normalize_bars_rejects_naive_timestamps() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h"))

    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_bars(data, symbol="AAA")


def test_normalize_bars_rejects_ambiguous_symbol_input() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))
    data["symbol"] = "AAA"

    with pytest.raises(ValueError, match="either symbol or a symbol column"):
        normalize_bars(data, symbol="AAA")


def test_normalize_bars_rejects_duplicate_observations() -> None:
    timestamp = pd.Timestamp("2026-01-01", tz="UTC")
    data = _bars(pd.DatetimeIndex([timestamp, timestamp]))

    with pytest.raises(ValueError, match=r"unique \(symbol, datetime\)"):
        normalize_bars(data, symbol="AAA")
