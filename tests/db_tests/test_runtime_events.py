"""Persistence contract for the runtime_events operational audit trail."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args
from unittest.mock import MagicMock, patch

import pytest
from librae.core.executor import RuntimeEvent, RuntimeEventType, coalesce_runtime_events
from librae.db.timescale_writer import write_runtime_event


def test_schema_defines_idempotent_runtime_event_key() -> None:
    sql = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS runtime_events" in sql
    assert "ON runtime_events(run_id, ts, event_type, COALESCE(symbol, ''))" in sql
    assert "REFERENCES backtest_runs(run_id) ON DELETE CASCADE" in sql


def test_event_types_match_the_runtime_events_constraint() -> None:
    """The engine runs against fakes that have no CHECK, so nothing else
    notices an event type the durable table would refuse."""
    sql = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")
    constraint = sql.partition("CONSTRAINT chk_runtime_event_type")[2].partition(")")[0]

    assert set(re.findall(r"'([a-z_]+)'", constraint)) == set(get_args(RuntimeEventType))


@pytest.mark.parametrize("event_type", ["halted", "halt_reset", "group_failed"])
@patch("librae.db.timescale_writer.get_conn")
def test_write_runtime_event_passes_halt_events_through(
    mock_get_conn: MagicMock, event_type: str
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    write_runtime_event(run_id="run-1", ts=ts, event_type=event_type, detail={"reason": "x"})

    _, values = cursor.execute.call_args.args
    assert values == (ts, "run-1", event_type, None, '{"reason": "x"}')


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


@patch("librae.db.timescale_writer.get_conn")
def test_phase_details_are_coalesced_before_writer_identity(
    mock_get_conn: MagicMock,
) -> None:
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    mock_get_conn.return_value = connection
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    events = coalesce_runtime_events(
        [
            RuntimeEvent(
                ts=ts,
                event_type="decision_skipped",
                symbol="A",
                detail={"reason": "rebalance_residual", "phase": "reduction"},
            ),
            RuntimeEvent(
                ts=ts,
                event_type="decision_skipped",
                symbol="A",
                detail={"reason": "rebalance_residual", "phase": "addition"},
            ),
        ]
    )

    for event in events:
        write_runtime_event(
            run_id="run-1",
            ts=event.ts,
            event_type=event.event_type,
            symbol=event.symbol,
            detail=event.detail,
        )

    assert len(events) == 1
    assert events[0].detail["phase"] == "reduction"
    assert events[0].detail["related_events"] == [
        {"reason": "rebalance_residual", "phase": "addition"}
    ]
    assert cursor.execute.call_count == 1
