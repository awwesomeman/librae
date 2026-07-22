"""Tests for strategies.module.data.tw_market_flow — market-wide margin/short
balance & institutional net-buy factor registration and the DB-cached
fetch_*_history() wrappers."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from strategies.module.data.factors import _FACTOR_FETCHERS
from strategies.module.data.tw_market_flow import (
    fetch_tw_margin_balance_history,
    fetch_tw_market_net_buy_history,
)

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_margin_and_short_balance_registered():
    assert "tw_market_margin_balance" in _FACTOR_FETCHERS
    assert "tw_market_short_balance" in _FACTOR_FETCHERS
    _fn, source, instrument_type, _freq = _FACTOR_FETCHERS["tw_market_margin_balance"]
    assert source == "finmind"
    assert instrument_type == "spot"


@pytest.mark.parametrize("institution", ["dealer", "trust", "foreign", "total"])
def test_market_net_buy_registered(institution):
    assert f"tw_market_{institution}_net_buy" in _FACTOR_FETCHERS


# ---------------------------------------------------------------------------
# fetch_*_history wrappers — thin renames over get_factor
# ---------------------------------------------------------------------------

def test_fetch_tw_margin_balance_history_rejects_unknown_factor():
    with pytest.raises(ValueError, match="factor must be"):
        fetch_tw_margin_balance_history("tw_something_else", "2024-01-01", "2024-01-02")


def test_fetch_tw_margin_balance_history_uses_pseudo_symbol():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [9391953.0]})
    with patch("strategies.module.data.tw_market_flow.get_factor", return_value=fake) as mock_get:
        result = fetch_tw_margin_balance_history("tw_market_margin_balance", "2024-01-01", "2024-01-02")
    mock_get.assert_called_once_with("TWSE", "tw_market_margin_balance", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "balance"]


def test_fetch_tw_market_net_buy_history_uses_pseudo_symbol():
    fake = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01T00:00:00Z"]), "value": [-5000.0]})
    with patch("strategies.module.data.tw_market_flow.get_factor", return_value=fake) as mock_get:
        result = fetch_tw_market_net_buy_history("foreign", "2024-01-01", "2024-01-02")
    mock_get.assert_called_once_with("TWSE", "tw_market_foreign_net_buy", start="2024-01-01", end="2024-01-02")
    assert list(result.columns) == ["timestamp", "net_buy"]


def test_fetch_tw_market_net_buy_history_rejects_unknown_institution():
    with pytest.raises(ValueError, match="institution must be one of"):
        fetch_tw_market_net_buy_history("retail", "2024-01-01", "2024-01-02")


# ---------------------------------------------------------------------------
# Row-shape transforms (real filter/net logic, mocked HTTP layer)
# ---------------------------------------------------------------------------

def test_margin_balance_fetcher_reads_today_balance():
    from strategies.module.data.tw_market_flow import _margin_balance_fetcher

    raw_rows = [
        {"date": "2024-01-01", "name": "MarginPurchase", "TodayBalance": 9391953},
        {"date": "2024-01-01", "name": "ShortSale", "TodayBalance": 234867},
    ]
    fetch_fn = _margin_balance_fetcher("MarginPurchase")
    with patch("strategies.module.data.tw_market_flow.finmind_client.fetch", return_value=raw_rows):
        result = fetch_fn("TWSE", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result["value"].tolist() == [9391953]


def test_market_net_buy_fetcher_computes_buy_minus_sell():
    from strategies.module.data.tw_market_flow import _market_net_buy_fetcher

    raw_rows = [
        {"date": "2024-01-01", "name": "Foreign_Investor", "buy": 617880515906, "sell": 581063143954},
        {"date": "2024-01-01", "name": "Investment_Trust", "buy": 49360345056, "sell": 41601714909},
    ]
    fetch_fn = _market_net_buy_fetcher("Foreign_Investor")
    with patch("strategies.module.data.tw_market_flow.finmind_client.fetch", return_value=raw_rows):
        result = fetch_fn("TWSE", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result["value"].tolist() == [617880515906 - 581063143954]


def test_empty_rows_return_empty_frame_with_correct_columns():
    from strategies.module.data.tw_market_flow import _margin_balance_fetcher

    fetch_fn = _margin_balance_fetcher("MarginPurchase")
    with patch("strategies.module.data.tw_market_flow.finmind_client.fetch", return_value=[]):
        result = fetch_fn("TWSE", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]


# ---------------------------------------------------------------------------
# attach_tw_market_flow_features
# ---------------------------------------------------------------------------

def test_attach_tw_market_flow_features_fills_missing_with_zero():
    from strategies.module.data.tw_market_flow import attach_tw_market_flow_features

    ohlcv = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-02T00:00:00Z"]), "close": [100.0]})
    empty = pd.DataFrame(columns=["timestamp", "value"])

    with patch("strategies.module.data.tw_market_flow.get_factor", return_value=empty):
        result = attach_tw_market_flow_features(ohlcv, "2024-01-01", "2024-01-03")

    assert result["tw_market_margin_balance"].tolist() == [0.0]
    assert result["tw_market_short_balance"].tolist() == [0.0]
    for institution in ("dealer", "trust", "foreign", "total"):
        assert result[f"tw_market_{institution}_net_buy"].tolist() == [0.0]
