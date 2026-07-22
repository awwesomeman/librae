"""Tests for strategies.module.data.quarterly_futures — quarterly_* factor
registration reuses open_interest.py's per-column fetcher under
instrument_type='contract_quarterly'."""
from __future__ import annotations

import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.open_interest import _COLUMNS
import strategies.module.data.quarterly_futures  # noqa: F401  (registers factor fetchers)


@pytest.mark.parametrize("factor_name", [f"quarterly_{name}" for name in _COLUMNS])
def test_quarterly_factors_registered_with_archive_source(factor_name):
    assert factor_name in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS[factor_name]
    assert source == "data.binance.vision"
    assert instrument_type == "contract_quarterly"
    assert freq == "M5"


def test_quarterly_open_interest_reuses_metrics_column_fetcher():
    """Same fetcher function backs both the perpetual and quarterly
    registrations for a given metric — no duplicated fetch logic."""
    _fn, _source, _instrument_type, _freq = _FACTOR_FETCHERS["quarterly_open_interest"]
    assert _fn.__qualname__.endswith("_metrics_column_fetcher.<locals>._fetch")
