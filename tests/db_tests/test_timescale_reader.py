"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from librae.backtest.schema import RunMetadata
from librae.db.timescale_reader import get_run


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


class TestGetRun:
    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_run_metadata_by_run_id(self, mock_conn_ctx):
        mock_cur = MagicMock()
        started_at = datetime(2026, 7, 29, tzinfo=UTC)
        ended_at = datetime(2026, 7, 30, tzinfo=UTC)
        run_at = datetime(2026, 7, 30, 1, tzinfo=UTC)
        mock_cur.fetchone.return_value = (
            "demo-20260729t1200-abcdef",
            "demo",
            '["BTCUSDT"]',
            "1h",
            "fixture",
            started_at,
            ended_at,
            run_at,
            "backtest",
        )
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_run("demo-20260729t1200-abcdef")

        assert result == RunMetadata(
            run_id="demo-20260729t1200-abcdef",
            strategy="demo",
            symbols=("BTCUSDT",),
            timeframe="1h",
            data_source="fixture",
            started_at=started_at,
            ended_at=ended_at,
            run_at=run_at,
            mode="backtest",
        )
        sql = mock_cur.execute.call_args[0][0]
        assert "WHERE run_id = %s" in sql

    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_none_when_not_found(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run("missing-run") is None
