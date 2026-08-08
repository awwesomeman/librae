"""Persistence contract for the runtime_events operational audit trail."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from librae.db.timescale_writer import write_runtime_event


def test_schema_defines_idempotent_runtime_event_key() -> None:
    sql = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS runtime_events" in sql
    assert "ON runtime_events(run_id, ts, event_type, COALESCE(symbol, ''))" in sql
    assert "REFERENCES backtest_runs(run_id) ON DELETE CASCADE" in sql


@patch("librae.db.timescale_writer.get_conn")
def test_write_runtime_event_upserts_same_event(mock_get_conn: MagicMock) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    write_runtime_event(
        run_id="run-1",
        ts=ts,
        event_type="decision_skipped",
        symbol="BTCUSDT",
        detail={"reason": "insufficient_cash"},
    )

    sql, values = cursor.execute.call_args.args
    assert "ON CONFLICT (run_id, ts, event_type, COALESCE(symbol, '')) DO UPDATE" in sql
    assert values == (
        ts,
        "run-1",
        "decision_skipped",
        "BTCUSDT",
        '{"reason": "insufficient_cash"}',
    )


@patch("librae.db.timescale_writer.get_conn")
def test_write_runtime_event_allows_missing_symbol_and_detail(mock_get_conn: MagicMock) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    write_runtime_event(run_id="run-1", ts=ts, event_type="state_recovered")

    _, values = cursor.execute.call_args.args
    assert values == (ts, "run-1", "state_recovered", None, None)
