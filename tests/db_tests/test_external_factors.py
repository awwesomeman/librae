"""Tests for external_factors / external_factor_coverage_ranges read/write —
the generic third-party-factor cache (funding rate, open interest, ...).
Mirrors test_ohlcv_coverage.py + write_ohlcv's coverage since it's the same
DB-first + gap-tracked-cache design, generalized to (symbol, factor_name,
source) instead of (symbol, timeframe, data_source)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from db.timescale_reader import get_external_factor_coverage_ranges, load_external_factor
from db.timescale_writer import merge_external_factor_coverage_ranges, write_external_factor


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


class TestGetExternalFactorCoverage:
    @patch("db.timescale_reader.get_conn")
    def test_returns_sorted_ranges(self, mock_conn_ctx):
        mock_cur = MagicMock()
        r1 = (datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [r1]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_external_factor_coverage_ranges("BTCUSDT", "funding_rate", "binanceusdm")

        assert result == [r1]
        sql = mock_cur.execute.call_args[0][0]
        assert "external_factor_coverage_ranges" in sql


class TestMergeExternalFactorCoverage:
    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_merges_touching_ranges(self, mock_conn_ctx, mock_exec_values):
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_external_factor_coverage_ranges(
            "BTCUSDT", "funding_rate", "binanceusdm",
            datetime(2024, 1, 2, tzinfo=timezone.utc), datetime(2024, 1, 3, tzinfo=timezone.utc),
        )

        delete_calls = [c for c in mock_cur.execute.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == ([1],)

        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 1
        assert inserted_rows[0][3] == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert inserted_rows[0][4] == datetime(2024, 1, 3, tzinfo=timezone.utc)

    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_keeps_disjoint_ranges_separate(self, mock_conn_ctx, mock_exec_values):
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_external_factor_coverage_ranges(
            "BTCUSDT", "funding_rate", "binanceusdm",
            datetime(2024, 1, 10, tzinfo=timezone.utc), datetime(2024, 1, 11, tzinfo=timezone.utc),
        )

        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 2


class TestWriteExternalFactor:
    def test_empty_df_writes_nothing(self):
        assert write_external_factor(pd.DataFrame(), "BTCUSDT", "funding_rate", "binanceusdm") == 0

    def test_naive_timestamp_raises(self):
        df = pd.DataFrame({"timestamp": pd.to_datetime(["2024-01-01"]), "value": [0.01]})
        import pytest
        with pytest.raises(ValueError, match="timezone-naive"):
            write_external_factor(df, "BTCUSDT", "funding_rate", "binanceusdm")

    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_writes_expected_rows(self, mock_conn_ctx, mock_exec_values):
        mock_cur = MagicMock()
        mock_conn_ctx.return_value = _mock_conn(mock_cur)
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-01-01T00:00:00Z", "2024-01-01T08:00:00Z"]),
            "value": [0.0001, -0.0002],
        })

        n = write_external_factor(df, "BTC/USDT:USDT", "funding_rate", "binanceusdm")

        assert n == 2
        rows = mock_exec_values.call_args[0][2]
        assert rows[0][1:4] == ("BTC/USDT:USDT", "funding_rate", "binanceusdm")


class TestLoadExternalFactor:
    @patch("db.timescale_reader.get_conn")
    def test_returns_tz_aware_frame(self, mock_conn_ctx):
        mock_conn_ctx.return_value.__enter__.return_value = MagicMock()
        with patch("db.timescale_reader.pd.read_sql") as mock_read_sql:
            mock_read_sql.return_value = pd.DataFrame({
                "timestamp": pd.to_datetime(["2024-01-01T00:00:00"]),
                "value": [0.0001],
            })
            result = load_external_factor("BTCUSDT", "open_interest", "data.binance.vision")

        assert result["timestamp"].dt.tz is not None
