"""Tests for strategies.module.data.us_chip — FINRA-sourced short positioning factors."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd

from strategies.module.data import us_chip  # noqa: F401  (registers factor fetchers)
from strategies.module.data.factors import _FACTOR_FETCHERS


def test_factors_registered():
    for factor_name, frequency in [("us_short_interest", "W2"), ("us_short_volume_ratio", "D1")]:
        assert factor_name in _FACTOR_FETCHERS
        _fn, source, instrument_type, freq = _FACTOR_FETCHERS[factor_name]
        assert source == "finra"
        assert instrument_type == "spot"
        assert freq == frequency


def test_short_interest_fetcher_reads_current_short_position():
    rows = [
        {"settlementDate": "2024-01-12", "currentShortPositionQuantity": 19985591},
        {"settlementDate": "2024-01-31", "currentShortPositionQuantity": 22095005},
    ]
    with patch("strategies.module.data.us_chip.finra_client.fetch", return_value=rows) as mock_fetch:
        result = us_chip._short_interest_fetcher("MU", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 2, 1, tzinfo=timezone.utc))

    assert result["value"].tolist() == [19985591.0, 22095005.0]
    body = mock_fetch.call_args[0][2]
    assert body["compareFilters"] == [{"fieldName": "symbolCode", "fieldValue": "MU", "compareType": "EQUAL"}]
    assert body["dateRangeFilters"][0]["startDate"] == "2024-01-01"


def test_short_interest_fetcher_empty_rows():
    with patch("strategies.module.data.us_chip.finra_client.fetch", return_value=[]):
        result = us_chip._short_interest_fetcher("MU", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 2, 1, tzinfo=timezone.utc))
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_short_volume_ratio_fetcher_sums_across_trfs():
    rows = [
        {"tradeReportDate": "2026-07-01", "shortParQuantity": 195380.24, "totalParQuantity": 474523.86, "reportingFacilityCode": "NCTRF"},
        {"tradeReportDate": "2026-07-01", "shortParQuantity": 10296732.12, "totalParQuantity": 20230279.68, "reportingFacilityCode": "NQTRF"},
        {"tradeReportDate": "2026-07-01", "shortParQuantity": 309629.74, "totalParQuantity": 646362.83, "reportingFacilityCode": "NYTRF"},
    ]
    with patch("strategies.module.data.us_chip.finra_client.fetch", return_value=rows):
        result = us_chip._short_volume_ratio_fetcher("MU", datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))

    assert len(result) == 1
    expected_ratio = (195380.24 + 10296732.12 + 309629.74) / (474523.86 + 20230279.68 + 646362.83)
    assert abs(result["value"].iloc[0] - expected_ratio) < 1e-9


def test_short_volume_ratio_fetcher_empty_rows():
    with patch("strategies.module.data.us_chip.finra_client.fetch", return_value=[]):
        result = us_chip._short_volume_ratio_fetcher("MU", datetime(2026, 7, 1, tzinfo=timezone.utc), datetime(2026, 7, 2, tzinfo=timezone.utc))
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


def test_attach_turnover_rate_divides_volume_by_shares_outstanding():
    ohlcv = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z"]),
        "volume": [11_290_000.0, 22_580_000.0],
    })
    shares_outstanding = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-06-25T00:00:00Z"]),
        "value": [1_129_000_000.0],
    })
    with patch("strategies.module.data.us_chip.get_factor", return_value=shares_outstanding):
        result = us_chip.attach_turnover_rate(ohlcv, "MU", "2026-01-01", "2026-07-22")

    assert "shares_outstanding" not in result.columns
    assert result["turnover_rate"].tolist() == [0.01, 0.02]


def test_attach_turnover_rate_zero_fills_when_no_shares_outstanding_data():
    ohlcv = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-07-01T00:00:00Z"]),
        "volume": [11_290_000.0],
    })
    with patch("strategies.module.data.us_chip.get_factor", return_value=pd.DataFrame(columns=["timestamp", "value"])):
        result = us_chip.attach_turnover_rate(ohlcv, "MU", "2026-01-01", "2026-07-22")

    assert result["turnover_rate"].tolist() == [0.0]
