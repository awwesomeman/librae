"""Tests for strategies.module.data.tw_futures_chip — futures/options chip &
liquidity factor registration and the DB-cached fetch_*_history() wrappers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.tw_futures_chip import (
    fetch_twfut_dealer_mm_volume_history,
    fetch_twfut_net_oi_history,
    fetch_twopt_net_oi_history,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("institution", ["dealer", "trust", "foreign"])
def test_futures_net_oi_registered(institution):
    factor_name = f"twfut_{institution}_net_oi"
    assert factor_name in _FACTOR_FETCHERS
    _fn, source, instrument_type = _FACTOR_FETCHERS[factor_name]
    assert source == "finmind"
    assert instrument_type == "contract_monthly"


@pytest.mark.parametrize("institution", ["dealer", "trust", "foreign"])
@pytest.mark.parametrize("call_put", ["call", "put"])
def test_option_net_oi_registered(institution, call_put):
    assert f"twopt_{institution}_{call_put}_net_oi" in _FACTOR_FETCHERS


def test_dealer_mm_volume_registered():
    assert "twfut_dealer_mm_volume" in _FACTOR_FETCHERS


# ---------------------------------------------------------------------------
# fetch_*_history wrappers — thin renames over get_factor
# ---------------------------------------------------------------------------

def test_fetch_twfut_net_oi_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [1234.0]})
    with patch("strategies.module.data.tw_futures_chip.get_factor", return_value=fake) as mock_get:
        result = fetch_twfut_net_oi_history("TXFR1", "foreign", "2024-01-01", "2024-01-02")
    mock_get.assert_called_once_with("TXFR1", "twfut_foreign_net_oi", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "net_oi"]


def test_fetch_twfut_net_oi_history_rejects_unknown_institution():
    with pytest.raises(ValueError, match="institution must be one of"):
        fetch_twfut_net_oi_history("TXFR1", "retail", "2024-01-01", "2024-01-02")


def test_fetch_twopt_net_oi_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [77.0]})
    with patch("strategies.module.data.tw_futures_chip.get_factor", return_value=fake) as mock_get:
        result = fetch_twopt_net_oi_history("TXFR1", "trust", "call", "2024-01-01", "2024-01-02")
    mock_get.assert_called_once_with("TXFR1", "twopt_trust_call_net_oi", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "net_oi"]


def test_fetch_twopt_net_oi_history_rejects_unknown_call_put():
    with pytest.raises(ValueError, match="call_put must be"):
        fetch_twopt_net_oi_history("TXFR1", "trust", "straddle", "2024-01-01", "2024-01-02")


def test_fetch_twfut_dealer_mm_volume_history_renames_value_column():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [10000.0]})
    with patch("strategies.module.data.tw_futures_chip.get_factor", return_value=fake) as mock_get:
        result = fetch_twfut_dealer_mm_volume_history("TXFR1", "2024-01-01", "2024-01-02")
    mock_get.assert_called_once_with("TXFR1", "twfut_dealer_mm_volume", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "dealer_mm_volume"]


# ---------------------------------------------------------------------------
# Row-shape transforms (real filter/net logic, mocked HTTP layer)
# ---------------------------------------------------------------------------

def test_futures_net_oi_fetcher_filters_by_institution_label():
    from strategies.module.data.tw_futures_chip import _futures_net_oi_fetcher

    raw_rows = [
        {"date": "2024-01-01", "institutional_investors": "外資",
         "long_open_interest_balance_volume": 100, "short_open_interest_balance_volume": 40},
        {"date": "2024-01-01", "institutional_investors": "自營商",
         "long_open_interest_balance_volume": 10, "short_open_interest_balance_volume": 5},
    ]
    fetch_fn = _futures_net_oi_fetcher("foreign")
    with patch("strategies.module.data.tw_futures_chip.finmind_client.fetch", return_value=raw_rows) as mock_fetch:
        result = fetch_fn("TXFR1", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert mock_fetch.call_args.kwargs["data_id"] == "TX"
    assert result["value"].tolist() == [60]


def test_option_net_oi_fetcher_filters_by_institution_and_call_put():
    from strategies.module.data.tw_futures_chip import _option_net_oi_fetcher

    raw_rows = [
        {"date": "2024-01-01", "institutional_investors": "外資", "call_put": "買權",
         "long_open_interest_balance_volume": 200, "short_open_interest_balance_volume": 150},
        {"date": "2024-01-01", "institutional_investors": "外資", "call_put": "賣權",
         "long_open_interest_balance_volume": 50, "short_open_interest_balance_volume": 80},
    ]
    fetch_call = _option_net_oi_fetcher("foreign", "call")
    fetch_put = _option_net_oi_fetcher("foreign", "put")
    with patch("strategies.module.data.tw_futures_chip.finmind_client.fetch", return_value=raw_rows) as mock_fetch:
        call_result = fetch_call("TXFR1", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        put_result = fetch_put("TXFR1", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert mock_fetch.call_args.kwargs["data_id"] == "TXO"
    assert call_result["value"].tolist() == [50]
    assert put_result["value"].tolist() == [-30]


def test_dealer_mm_volume_fetcher_sums_across_dealer_codes():
    from strategies.module.data.tw_futures_chip import _dealer_mm_volume_fetcher

    raw_rows = [
        {"date": "2024-01-01", "dealer_code": "F040999", "volume": 100, "is_after_hour": True},
        {"date": "2024-01-01", "dealer_code": "S218999", "volume": 250, "is_after_hour": False},
        {"date": "2024-01-02", "dealer_code": "F040999", "volume": 50, "is_after_hour": False},
    ]
    with patch("strategies.module.data.tw_futures_chip.finmind_client.fetch", return_value=raw_rows) as mock_fetch:
        result = _dealer_mm_volume_fetcher("TXFR1", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert mock_fetch.call_args.kwargs["data_id"] == "TX"
    assert result["value"].tolist() == [350, 50]


def test_empty_rows_return_empty_frame_with_correct_columns():
    from strategies.module.data.tw_futures_chip import _futures_net_oi_fetcher

    fetch_fn = _futures_net_oi_fetcher("dealer")
    with patch("strategies.module.data.tw_futures_chip.finmind_client.fetch", return_value=[]):
        result = fetch_fn("TXFR1", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


# ---------------------------------------------------------------------------
# attach_tw_futures_chip_features
# ---------------------------------------------------------------------------

def test_attach_tw_futures_chip_features_fills_missing_with_zero():
    from strategies.module.data.tw_futures_chip import attach_tw_futures_chip_features

    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-02T00:00:00Z"]), "close": [100.0]})
    empty = pd.DataFrame(columns=["timestamp", "value"])

    with patch("strategies.module.data.tw_futures_chip.get_factor", return_value=empty):
        result = attach_tw_futures_chip_features(ohlcv, "TXFR1", "2024-01-01", "2024-01-03")

    for institution in ("dealer", "trust", "foreign"):
        assert result[f"twfut_{institution}_net_oi"].tolist() == [0.0]
        for call_put in ("call", "put"):
            assert result[f"twopt_{institution}_{call_put}_net_oi"].tolist() == [0.0]
    assert result["twfut_dealer_mm_volume"].tolist() == [0.0]
