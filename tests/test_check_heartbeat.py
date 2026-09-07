"""Offline contract tests for the standalone heartbeat watchdog."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from scripts.check_heartbeat import check_and_alert, find_stale_runs


def _connection_with_rows(rows: list[tuple]) -> tuple[MagicMock, MagicMock]:
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    connection = MagicMock()
    connection.cursor.return_value = cursor
    context = MagicMock()
    context.__enter__.return_value = connection
    return context, cursor


def test_find_stale_runs_uses_current_schema_and_poll_contract() -> None:
    heartbeat = datetime(2026, 9, 7, tzinfo=UTC)
    connection, cursor = _connection_with_rows(
        [("run-1", "pairs", ["BTCUSDT", "ETHUSDT"], "live", 20, heartbeat)]
    )

    with patch("scripts.check_heartbeat.get_conn", return_value=connection):
        result = find_stale_runs()

    sql = cursor.execute.call_args.args[0]
    assert "strategy_name, symbols" in sql
    assert "strategy, symbol" not in sql
    assert "poll_seconds > 0" in sql
    assert "make_interval(secs => poll_seconds * %s)" in sql
    assert cursor.execute.call_args.args[1] == (3,)
    assert result == [
        {
            "run_id": "run-1",
            "strategy": "pairs",
            "symbols": "BTCUSDT, ETHUSDT",
            "mode": "live",
            "poll_seconds": 20,
            "last_heartbeat_at": heartbeat.isoformat(),
        }
    ]
    cursor.close.assert_called_once_with()


def test_check_and_alert_reports_the_complete_symbol_set() -> None:
    adapter = MagicMock()
    stale = {
        "run_id": "run-1",
        "strategy": "rotation",
        "symbols": "AAA, BBB, CCC",
        "mode": "sim",
        "poll_seconds": 60,
        "last_heartbeat_at": "2026-09-07T00:00:00+00:00",
    }

    with patch("scripts.check_heartbeat.find_stale_runs", return_value=[stale]):
        assert check_and_alert(adapter) == 1

    message = adapter.send_alert.call_args.kwargs["message"]
    assert "Symbols: AAA, BBB, CCC" in message
