"""Tests for data.ohlcv — unified OHLCV fetching with coverage-tracked caching."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategies.module.data.ohlcv import (
    OHLCV_COLUMNS,
    _ccxt_fetcher,
    _compute_gaps,
    _resolve_instrument_type,
    _shioaji_fetcher,
    get_ohlcv,
    register_ohlcv_fetcher,
)
from librae.core.utils import interval_to_timedelta

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 2, 1, tzinfo=timezone.utc)


def _make_ohlcv_df(n: int = 5, start_ts: datetime | None = None) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame for testing."""
    start = start_ts or START
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": range(n),
        "high": [x + 1 for x in range(n)],
        "low": [max(0, x - 1) for x in range(n)],
        "close": [x + 0.5 for x in range(n)],
        "volume": [100.0] * n,
    })


def _make_ccxt_page(n: int = 5, start_ts: datetime | None = None) -> pd.DataFrame:
    """Build a DataFrame shaped like CryptoAdapter.fetch_ohlcv's output (``ts`` column)."""
    return _make_ohlcv_df(n, start_ts).rename(columns={"timestamp": "ts"})


class TestGetOhlcv:
    """get_ohlcv coverage-first with API gap-fill."""

    @patch("strategies.module.data.ohlcv._merge_coverage")
    @patch("strategies.module.data.ohlcv._upsert_db")
    @patch("strategies.module.data.ohlcv._fetch_from_api")
    @patch("strategies.module.data.ohlcv._query_db")
    @patch("strategies.module.data.ohlcv._query_coverage")
    def test_fully_covered_skips_api(self, mock_cov, mock_db, mock_api, mock_upsert, mock_merge):
        """Fully covered range → return DB data, never call API."""
        mock_cov.return_value = [(START, START + pd.Timedelta(hours=799))]
        mock_db.return_value = _make_ohlcv_df(800, start_ts=START)

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot",
                           start=START, end=START + pd.Timedelta(hours=799))

        assert len(result) == 800
        mock_api.assert_not_called()
        mock_upsert.assert_not_called()
        mock_merge.assert_not_called()

    @patch("strategies.module.data.ohlcv._merge_coverage")
    @patch("strategies.module.data.ohlcv._upsert_db")
    @patch("strategies.module.data.ohlcv._fetch_from_api")
    @patch("strategies.module.data.ohlcv._query_db")
    @patch("strategies.module.data.ohlcv._query_coverage")
    def test_no_coverage_fetches_api_and_upserts(self, mock_cov, mock_db, mock_api, mock_upsert, mock_merge):
        """No coverage → fetch full range from API, upsert, mark covered, re-read from DB."""
        mock_cov.return_value = []
        api_df = _make_ohlcv_df(5)
        mock_db.return_value = api_df  # re-read after upsert
        mock_api.return_value = api_df

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start=START, end=END)

        mock_api.assert_called_once()
        mock_upsert.assert_called_once()
        mock_merge.assert_called_once_with("BTCUSDT", "1h", "binance_spot", START, END, "spot")
        assert len(result) == 5

    @patch("strategies.module.data.ohlcv._merge_coverage")
    @patch("strategies.module.data.ohlcv._upsert_db")
    @patch("strategies.module.data.ohlcv._fetch_from_api")
    @patch("strategies.module.data.ohlcv._query_db")
    @patch("strategies.module.data.ohlcv._query_coverage")
    def test_disjoint_coverage_only_fetches_the_gap(self, mock_cov, mock_db, mock_api, mock_upsert, mock_merge):
        """A range disjoint from what's cached fetches only the missing slice, not the whole span."""
        cached_start = END - pd.Timedelta(days=1)
        mock_cov.return_value = [(cached_start, END)]  # only the tail end is cached
        mock_db.return_value = _make_ohlcv_df(5)
        mock_api.return_value = _make_ohlcv_df(5)

        get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start=START, end=END)

        mock_api.assert_called_once_with("BTCUSDT", "1h", START, cached_start, "binance_spot")

    @patch("strategies.module.data.ohlcv._fetch_from_api")
    @patch("strategies.module.data.ohlcv._query_coverage")
    def test_coverage_unavailable_falls_back_to_api(self, mock_cov, mock_api):
        """DB completely unavailable → return API data directly, no caching attempted."""
        mock_cov.return_value = None
        mock_api.return_value = _make_ohlcv_df(3)

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start=START, end=END)

        mock_api.assert_called_once()
        assert len(result) == 3
        assert list(result.columns) == OHLCV_COLUMNS

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError, match="No OHLCV fetcher"):
            get_ohlcv("BTCUSDT", "1h", data_source="nonexistent_exchange", start=START)

    @patch("strategies.module.data.ohlcv._merge_coverage")
    @patch("strategies.module.data.ohlcv._upsert_db")
    @patch("strategies.module.data.ohlcv._query_db")
    @patch("strategies.module.data.ohlcv._query_coverage")
    def test_custom_fetcher_registration(self, mock_cov, mock_db, mock_upsert, mock_merge):
        """Registered custom fetcher is used for its source name."""
        custom_df = _make_ohlcv_df(2)
        mock_fetcher = MagicMock(return_value=custom_df)
        register_ohlcv_fetcher("test_exchange", mock_fetcher)

        mock_cov.return_value = []
        mock_db.return_value = custom_df

        result = get_ohlcv("SYMBOL", "1h", data_source="test_exchange", start=START, end=END)

        mock_fetcher.assert_called_once()
        assert len(result) == 2


class TestComputeGaps:
    """_compute_gaps boundary detection over disjoint coverage ranges."""

    def test_no_gaps_when_fully_covered(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        assert _compute_gaps([(start, end)], start, end) == []

    def test_gap_at_start(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        covered_from = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)

        gaps = _compute_gaps([(covered_from, end)], start, end)
        assert gaps == [(start, covered_from)]

    def test_gap_at_end(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 3, tzinfo=timezone.utc)
        covered_to = datetime(2024, 1, 2, tzinfo=timezone.utc)

        gaps = _compute_gaps([(start, covered_to)], start, end)
        assert gaps == [(covered_to, end)]

    def test_no_coverage_returns_full_range(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        assert _compute_gaps([], start, end) == [(start, end)]

    def test_disjoint_coverage_only_returns_middle_gap(self):
        """Old + new cached islands with a real gap between them must not
        span the whole request, unlike a naive min/max heuristic."""
        start = datetime(2020, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, tzinfo=timezone.utc)
        old_island = (datetime(2019, 1, 1, tzinfo=timezone.utc), datetime(2020, 1, 15, tzinfo=timezone.utc))
        new_island = (datetime(2023, 12, 1, tzinfo=timezone.utc), datetime(2025, 1, 1, tzinfo=timezone.utc))

        gaps = _compute_gaps([old_island, new_island], start, end)

        assert gaps == [(old_island[1], new_island[0])]


class TestCcxtFetcher:
    """_ccxt_fetcher — pagination wrapper for CryptoAdapter (ccxt)."""

    @patch("brokers.crypto_adapter.CryptoAdapter")
    def test_single_short_page(self, mock_adapter_cls):
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.fetch_ohlcv.return_value = _make_ccxt_page(5, start_ts=START)

        fetch = _ccxt_fetcher("binance")
        result = fetch("BTCUSDT", "1h", START, START + pd.Timedelta(hours=4))

        mock_adapter_cls.assert_called_once_with(exchange_id="binance")
        assert list(result.columns) == OHLCV_COLUMNS
        assert len(result) == 5
        assert mock_adapter.fetch_ohlcv.call_count == 1

    @patch("brokers.crypto_adapter.CryptoAdapter")
    def test_paginates_full_pages(self, mock_adapter_cls):
        """A full page (== limit) triggers another fetch; a short page stops."""
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        page1 = _make_ccxt_page(1000, start_ts=START)
        page2 = _make_ccxt_page(5, start_ts=START + pd.Timedelta(hours=1000))
        mock_adapter.fetch_ohlcv.side_effect = [page1, page2]

        fetch = _ccxt_fetcher("binance")
        result = fetch("BTCUSDT", "1h", START, START + pd.Timedelta(hours=1010))

        assert mock_adapter.fetch_ohlcv.call_count == 2
        assert len(result) == 1005

    @patch("brokers.crypto_adapter.CryptoAdapter")
    def test_empty_result_returns_empty_frame(self, mock_adapter_cls):
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.fetch_ohlcv.return_value = pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume"],
        )

        fetch = _ccxt_fetcher("binance")
        result = fetch("BTCUSDT", "1h", START, END)

        assert result.empty
        assert list(result.columns) == OHLCV_COLUMNS


class TestShioajiFetcher:
    """_shioaji_fetcher — lazy-login wrapper for ShioajiAdapter."""

    @patch("brokers.shioaji_adapter.ShioajiAdapter")
    def test_returns_correct_columns_and_range_filtered(self, mock_adapter_cls):
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.fetch_ohlcv.return_value = _make_ccxt_page(5, start_ts=START)

        fetch = _shioaji_fetcher()
        result = fetch("TXFR1", "5m", START, START + pd.Timedelta(hours=4))

        assert list(result.columns) == OHLCV_COLUMNS
        assert len(result) == 5
        mock_adapter.fetch_ohlcv.assert_called_once_with(
            "TXFR1", "5m", start=START, end=START + pd.Timedelta(hours=4),
        )

    @patch("brokers.shioaji_adapter.ShioajiAdapter")
    def test_adapter_created_once_and_reused_across_calls(self, mock_adapter_cls):
        """Login is expensive — one adapter instance per fetcher, not per call."""
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.fetch_ohlcv.return_value = _make_ccxt_page(2, start_ts=START)

        fetch = _shioaji_fetcher()
        fetch("TXFR1", "5m", START, END)
        fetch("TXFR1", "5m", START, END)

        mock_adapter_cls.assert_called_once_with(simulation=True)
        assert mock_adapter.fetch_ohlcv.call_count == 2

    @patch("brokers.shioaji_adapter.ShioajiAdapter")
    def test_empty_result_returns_empty_frame(self, mock_adapter_cls):
        mock_adapter = MagicMock()
        mock_adapter_cls.return_value = mock_adapter
        mock_adapter.fetch_ohlcv.return_value = pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume"],
        )

        fetch = _shioaji_fetcher()
        result = fetch("TXFR1", "5m", START, END)

        assert result.empty
        assert list(result.columns) == OHLCV_COLUMNS

    def test_registered_under_shioaji(self):
        from strategies.module.data.ohlcv import _OHLCV_FETCHERS
        assert "shioaji" in _OHLCV_FETCHERS


class TestResolveInstrumentType:
    def test_registered_symbol_returns_registry_value(self):
        assert _resolve_instrument_type("BTCUSDT") == "spot"
        assert _resolve_instrument_type("TXFR1") == "contract_monthly"

    def test_unregistered_symbol_falls_back_to_spot(self, caplog):
        assert _resolve_instrument_type("SOME_EXPERIMENT_TICKER") == "spot"


class TestIntervalToTimedelta:
    def test_1h(self):
        assert interval_to_timedelta("1h") == pd.Timedelta(hours=1)

    def test_5m(self):
        assert interval_to_timedelta("5m") == pd.Timedelta(minutes=5)

    def test_1d(self):
        assert interval_to_timedelta("1d") == pd.Timedelta(days=1)

    def test_canonical_H1(self):
        assert interval_to_timedelta("H1") == pd.Timedelta(hours=1)
