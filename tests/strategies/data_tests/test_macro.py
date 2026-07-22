"""Tests for strategies.module.data.macro — FRED/NY Fed-sourced macro regime factor registration."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from strategies.module.data import macro  # noqa: F401  (registers factor fetchers)
from strategies.module.data.factors import _FACTOR_FETCHERS

FRED_FACTORS = [
    "us_credit_spread", "us_financial_conditions", "us_mortgage_rate",
    "us_vix", "us_yield_curve_10y2y", "us_semiconductor_production",
]


@pytest.mark.parametrize("factor_name", FRED_FACTORS)
def test_fred_factors_registered(factor_name):
    assert factor_name in _FACTOR_FETCHERS
    _fn, source, instrument_type, _freq = _FACTOR_FETCHERS[factor_name]
    assert source == "fred"
    assert instrument_type == "spot"


def test_gscpi_factor_registered_under_nyfed_source():
    assert "us_supply_chain_pressure" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["us_supply_chain_pressure"]
    assert source == "nyfed"
    assert instrument_type == "spot"
    assert freq == "MN1"


def test_fred_series_fetcher_drops_missing_observations():
    from strategies.module.data.macro import _fred_series_fetcher

    raw_rows = [
        {"date": "2024-01-01", "value": "4.5"},
        {"date": "2024-01-02", "value": "."},  # FRED's no-release marker
    ]
    fetch_fn = _fred_series_fetcher("BAMLH0A0HYM2")
    with patch("strategies.module.data.macro.fred_client.fetch", return_value=raw_rows):
        result = fetch_fn("MACRO", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result["value"].tolist() == [4.5]


def test_fred_series_fetcher_empty_rows_return_empty_frame():
    from strategies.module.data.macro import _fred_series_fetcher

    fetch_fn = _fred_series_fetcher("NFCI")
    with patch("strategies.module.data.macro.fred_client.fetch", return_value=[]):
        result = fetch_fn("MACRO", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_gscpi_fetcher_filters_full_history_to_requested_range():
    from strategies.module.data.macro import _gscpi_fetcher

    full_history = pd.DataFrame({
        "date": pd.to_datetime(["1998-01-31", "2024-01-31", "2024-02-29"]),
        "value": [-1.09, 0.5, 0.6],
    })
    with patch("strategies.module.data.macro.nyfed_client.fetch", return_value=full_history):
        result = _gscpi_fetcher("MACRO", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 2, 1, tzinfo=timezone.utc))

    assert result["value"].tolist() == [0.5]
