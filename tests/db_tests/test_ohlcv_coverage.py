"""Tests for ohlcv_coverage_ranges read/write — coverage-range gap-fill cache."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from db.timescale_reader import get_ohlcv_coverage_ranges
from db.timescale_writer import merge_ohlcv_coverage_ranges


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


class TestGetOhlcvCoverage:
    @patch("db.timescale_reader.get_conn")
    def test_returns_sorted_ranges(self, mock_conn_ctx):
        mock_cur = MagicMock()
        r1 = (datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [r1]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_ohlcv_coverage_ranges("BTCUSDT", "H1", "binance_spot")

        assert result == [r1]
        sql = mock_cur.execute.call_args[0][0]
        assert "ohlcv_coverage_ranges" in sql


class TestMergeOhlcvCoverage:
    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_merges_touching_ranges(self, mock_conn_ctx, mock_exec_values):
        """New range touching an existing one should merge into a single row."""
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_ohlcv_coverage_ranges(
            "BTCUSDT", "H1", "binance_spot",
            datetime(2024, 1, 2, tzinfo=timezone.utc), datetime(2024, 1, 3, tzinfo=timezone.utc),
        )

        delete_calls = [c for c in mock_cur.execute.call_args_list if "DELETE" in c[0][0]]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == ([1],)

        mock_exec_values.assert_called_once()
        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 1
        assert inserted_rows[0][4] == datetime(2024, 1, 1, tzinfo=timezone.utc)
        assert inserted_rows[0][5] == datetime(2024, 1, 3, tzinfo=timezone.utc)

    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_keeps_disjoint_ranges_separate(self, mock_conn_ctx, mock_exec_values):
        """A new range with a real gap from the existing one stays as two rows."""
        mock_cur = MagicMock()
        existing = (1, datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
        mock_cur.fetchall.return_value = [existing]
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        merge_ohlcv_coverage_ranges(
            "BTCUSDT", "H1", "binance_spot",
            datetime(2024, 1, 10, tzinfo=timezone.utc), datetime(2024, 1, 11, tzinfo=timezone.utc),
        )

        inserted_rows = mock_exec_values.call_args[0][2]
        assert len(inserted_rows) == 2
