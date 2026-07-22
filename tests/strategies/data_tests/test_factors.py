"""Tests for strategies.module.data.factors — unified third-party factor fetching
with coverage-tracked caching. Mirrors test_ohlcv.py's TestGetOhlcv structure
since get_factor() shares the same DB-first + gap-fill design as get_ohlcv."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategies.module.data.factors import (
    FACTOR_COLUMNS,
    collect_snapshot_factor,
    get_factor,
    load_snapshot_factor,
    register_factor_fetcher,
)

START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 2, 1, tzinfo=timezone.utc)


def _make_factor_df(n: int = 5, start_ts: datetime | None = None) -> pd.DataFrame:
    start = start_ts or START
    ts = pd.date_range(start, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({"timestamp": ts, "value": [float(i) for i in range(n)]})


class TestGetFactor:
    @patch("strategies.module.data.factors._merge_coverage")
    @patch("strategies.module.data.factors._upsert_db")
    @patch("strategies.module.data.factors._query_db")
    @patch("strategies.module.data.factors._query_coverage")
    def test_fully_covered_skips_fetcher(self, mock_cov, mock_db, mock_upsert, mock_merge):
        mock_cov.return_value = [(START, END)]
        mock_db.return_value = _make_factor_df(5)
        mock_fetcher = MagicMock()
        register_factor_fetcher("test_factor_covered", mock_fetcher, source="test-source", frequency="D1")

        result = get_factor("BTCUSDT", "test_factor_covered", start=START, end=END)

        assert len(result) == 5
        mock_fetcher.assert_not_called()
        mock_upsert.assert_not_called()
        mock_merge.assert_not_called()

    @patch("strategies.module.data.factors._merge_coverage")
    @patch("strategies.module.data.factors._upsert_db")
    @patch("strategies.module.data.factors._query_db")
    @patch("strategies.module.data.factors._query_coverage")
    def test_no_coverage_fetches_and_upserts(self, mock_cov, mock_db, mock_upsert, mock_merge):
        mock_cov.return_value = []
        fetched = _make_factor_df(5)
        mock_db.return_value = fetched
        mock_fetcher = MagicMock(return_value=fetched)
        register_factor_fetcher("test_factor_nocov", mock_fetcher, source="test-source", frequency="D1")

        result = get_factor("BTCUSDT", "test_factor_nocov", start=START, end=END)

        mock_fetcher.assert_called_once_with("BTCUSDT", START, END)
        mock_upsert.assert_called_once()
        mock_merge.assert_called_once_with("BTCUSDT", "test_factor_nocov", "test-source", "spot", START, END)
        assert len(result) == 5

    @patch("strategies.module.data.factors._query_coverage")
    def test_coverage_unavailable_falls_back_to_fetcher(self, mock_cov):
        mock_cov.return_value = None
        fetched = _make_factor_df(3)
        mock_fetcher = MagicMock(return_value=fetched)
        register_factor_fetcher("test_factor_nodbfallback", mock_fetcher, source="test-source", frequency="D1")

        result = get_factor("BTCUSDT", "test_factor_nodbfallback", start=START, end=END)

        mock_fetcher.assert_called_once_with("BTCUSDT", START, END)
        assert len(result) == 3
        assert list(result.columns) == FACTOR_COLUMNS

    def test_unknown_factor_raises(self):
        with pytest.raises(ValueError, match="No fetcher registered"):
            get_factor("BTCUSDT", "nonexistent_factor", start=START)

    @patch("strategies.module.data.factors._merge_coverage")
    @patch("strategies.module.data.factors._upsert_db")
    @patch("strategies.module.data.factors._query_db")
    @patch("strategies.module.data.factors._query_coverage")
    def test_source_is_recorded_from_registration_not_caller(self, mock_cov, mock_db, mock_upsert, mock_merge):
        """source isn't a caller-facing param — it's whatever the fetcher was registered with."""
        mock_cov.return_value = []
        fetched = _make_factor_df(2)
        mock_db.return_value = fetched
        register_factor_fetcher("test_factor_source", MagicMock(return_value=fetched), source="my-provider", frequency="D1")

        get_factor("BTCUSDT", "test_factor_source", start=START, end=END)

        mock_merge.assert_called_once_with("BTCUSDT", "test_factor_source", "my-provider", "spot", START, END)


class TestSnapshotFactor:
    """collect_snapshot_factor/load_snapshot_factor — Path B, no coverage tracking."""

    @patch("db.timescale_writer.write_external_factor")
    @patch("db.timescale_writer.write_factor_registry")
    def test_collect_snapshot_factor_defaults_timestamp_to_now(self, mock_registry, mock_write):
        mock_write.return_value = 1
        with patch("strategies.module.data.factors.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
            written = collect_snapshot_factor("MU", "us_social_mentions", "apewisdom", 473, frequency="H1")

        assert written == 1
        mock_registry.assert_called_once_with([{"factor_name": "us_social_mentions", "source": "apewisdom", "frequency": "H1"}])
        df = mock_write.call_args[0][0]
        assert df["timestamp"].iloc[0] == pd.Timestamp("2026-07-22T12:00:00Z")
        assert df["value"].iloc[0] == 473.0

    @patch("db.timescale_writer.write_external_factor")
    @patch("db.timescale_writer.write_factor_registry")
    def test_collect_snapshot_factor_uses_explicit_ts_when_given(self, mock_registry, mock_write):
        mock_write.return_value = 1
        collect_snapshot_factor("MU", "us_earnings_surprise", "finnhub", 17.3, frequency="IRREGULAR", ts=datetime(2026, 6, 24, tzinfo=timezone.utc))

        df = mock_write.call_args[0][0]
        assert df["timestamp"].iloc[0] == pd.Timestamp("2026-06-24T00:00:00Z")

    @patch("db.timescale_reader.load_external_factor")
    def test_load_snapshot_factor_delegates_to_db_reader(self, mock_load):
        mock_load.return_value = _make_factor_df(2)
        result = load_snapshot_factor("MU", "us_social_mentions", "apewisdom", start="2026-01-01")

        mock_load.assert_called_once_with("MU", "us_social_mentions", "apewisdom", instrument_type="spot", started_at="2026-01-01", ended_at=None)
        assert len(result) == 2
