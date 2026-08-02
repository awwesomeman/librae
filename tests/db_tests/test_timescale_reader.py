"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
        mock_cur.fetchone.return_value = (
            "demo-20260729t1200-abcdef",
            '{"fast": 10}',
            '{"slippage_bps": 5}',
            None,
        )
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_run("demo-20260729t1200-abcdef")

        assert result == {
            "run_id": "demo-20260729t1200-abcdef",
            "params": {"fast": 10},
            "execution_policy": {"slippage_bps": 5},
            "risk_policy": None,
        }
        sql = mock_cur.execute.call_args[0][0]
        assert "WHERE run_id = %s" in sql

    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_none_when_not_found(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run("missing-run") is None
