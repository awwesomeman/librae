"""Tests for data.ohlcv — unified OHLCV fetching with DB-first caching."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from data.ohlcv import (
    OHLCV_COLUMNS,
    _find_gaps,
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


class TestGetOhlcv:
    """get_ohlcv DB-first with API gap-fill."""

    @patch("data.ohlcv._upsert_db")
    @patch("data.ohlcv._fetch_from_api")
    @patch("data.ohlcv._query_db")
    def test_db_hit_skips_api(self, mock_db, mock_api, mock_upsert):
        """Full DB hit → return DB data, never call API."""
        db_df = _make_ohlcv_df(800, start_ts=START)
        mock_db.return_value = db_df

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot",
                           start=START, end=START + pd.Timedelta(hours=799))

        assert len(result) == 800
        mock_api.assert_not_called()
        mock_upsert.assert_not_called()

    @patch("data.ohlcv._upsert_db")
    @patch("data.ohlcv._fetch_from_api")
    @patch("data.ohlcv._query_db")
    def test_db_miss_fetches_api_and_upserts(self, mock_db, mock_api, mock_upsert):
        """DB miss → fetch from API, upsert, re-read from DB."""
        api_df = _make_ohlcv_df(5)
        mock_db.side_effect = [pd.DataFrame(), api_df]  # miss, then hit after upsert
        mock_api.return_value = api_df

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start=START, end=END)

        mock_api.assert_called_once()
        mock_upsert.assert_called_once()
        assert len(result) == 5

    @patch("data.ohlcv._upsert_db")
    @patch("data.ohlcv._fetch_from_api")
    @patch("data.ohlcv._query_db")
    def test_db_unavailable_returns_api_data(self, mock_db, mock_api, mock_upsert):
        """DB completely unavailable → return API data directly."""
        api_df = _make_ohlcv_df(3)
        mock_db.return_value = None  # DB unavailable on both reads
        mock_api.return_value = api_df

        result = get_ohlcv("BTCUSDT", "1h", data_source="binance_spot", start=START, end=END)

        assert len(result) == 3
        assert list(result.columns) == OHLCV_COLUMNS

    def test_unknown_source_raises(self):
        with pytest.raises(ValueError, match="No OHLCV fetcher"):
            get_ohlcv("BTCUSDT", "1h", data_source="nonexistent_exchange", start=START)

    @patch("data.ohlcv._upsert_db")
    @patch("data.ohlcv._query_db")
    def test_custom_fetcher_registration(self, mock_db, mock_upsert):
        """Registered custom fetcher is used for its source name."""
        custom_df = _make_ohlcv_df(2)
        mock_fetcher = MagicMock(return_value=custom_df)
        register_ohlcv_fetcher("test_exchange", mock_fetcher)

        mock_db.side_effect = [pd.DataFrame(), custom_df]

        result = get_ohlcv("SYMBOL", "1h", data_source="test_exchange", start=START, end=END)

        mock_fetcher.assert_called_once()
        assert len(result) == 2


class TestFindGaps:
    """_find_gaps boundary detection."""

    def test_no_gaps_when_fully_covered(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        df = _make_ohlcv_df(24, start_ts=start)

        gaps = _find_gaps(df, start, end, "1h")
        assert gaps == []

    def test_gap_at_start(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)
        # DB only has data from hour 12 onwards
        late_start = datetime(2024, 1, 1, 12, tzinfo=timezone.utc)
        df = _make_ohlcv_df(12, start_ts=late_start)

        gaps = _find_gaps(df, start, end, "1h")
        assert len(gaps) >= 1
        assert gaps[0][0] == start

    def test_gap_at_end(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 3, tzinfo=timezone.utc)
        # DB only has first day
        df = _make_ohlcv_df(24, start_ts=start)

        gaps = _find_gaps(df, start, end, "1h")
        assert len(gaps) >= 1

    def test_empty_df_returns_full_range(self):
        start = datetime(2024, 1, 1, tzinfo=timezone.utc)
        end = datetime(2024, 1, 2, tzinfo=timezone.utc)

        gaps = _find_gaps(pd.DataFrame(), start, end, "1h")
        assert gaps == [(start, end)]


class TestIntervalToTimedelta:
    def test_1h(self):
        assert interval_to_timedelta("1h") == pd.Timedelta(hours=1)

    def test_5m(self):
        assert interval_to_timedelta("5m") == pd.Timedelta(minutes=5)

    def test_1d(self):
        assert interval_to_timedelta("1d") == pd.Timedelta(days=1)

    def test_canonical_H1(self):
        assert interval_to_timedelta("H1") == pd.Timedelta(hours=1)
