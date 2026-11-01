"""Timescale runtime-state adapter tests without a real database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from librae.db.timescale_state import TimescaleLiveStateStore
from librae.live.execution_identity import ExecutionIdentity
from librae.live.executor import OrderRequest
from librae.live.state import LiveRuntimeState, TrackedOrder


def _state() -> LiveRuntimeState:
    return LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        account_id="default",
        execution_identity=ExecutionIdentity("fixture", "paper", "fixture", "a" * 24),
        runtime_revision="revision-a",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    )


@patch("librae.db.timescale_state.psycopg2.extras.execute_values")
@patch("librae.db.timescale_state.get_conn")
def test_save_checkpoints_state_and_order_in_one_connection(mock_get_conn, mock_execute_values):
    conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = conn
    tracked = TrackedOrder(
        request=OrderRequest(
            client_order_id="client-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
            reference_price=100.0,
        ),
        placement_attempted=True,
        placement_attempted_at=datetime(2025, 1, 1, tzinfo=UTC),
        order_id="broker-1",
        status="accepted",
    )

    TimescaleLiveStateStore().save(_state(), [tracked])

    conn.cursor.return_value.execute.assert_called_once()
    rows = mock_execute_values.call_args.args[2]
    assert len(rows) == 1
    assert rows[0][1] == "client-1"
    assert rows[0][7] is True
    assert rows[0][8] == datetime(2025, 1, 1, tzinfo=UTC)
    assert rows[0][-1] is None


@patch("librae.db.timescale_state.psycopg2.extras.execute_values")
@patch("librae.db.timescale_state.get_conn")
def test_a_recorded_placement_error_survives_a_restored_order(mock_get_conn, mock_execute_values):
    """The checkpoint does not carry the error, so an order restored after a
    restart writes none; the row must keep the one already recorded."""
    mock_get_conn.return_value.__enter__.return_value = MagicMock()
    tracked = TrackedOrder(
        request=OrderRequest(
            client_order_id="client-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        placement_attempted=True,
        placement_attempted_at=datetime(2025, 1, 1, tzinfo=UTC),
        error="TimeoutError: read timed out",
    )
    store = TimescaleLiveStateStore()

    store.save(_state(), [tracked])
    assert mock_execute_values.call_args.args[2][0][-1] == "TimeoutError: read timed out"
    assert "error" not in tracked.to_dict()

    restored = TrackedOrder.from_dict(tracked.to_dict())
    store.save(_state(), [restored])

    sql = mock_execute_values.call_args.args[1]
    assert mock_execute_values.call_args.args[2][0][-1] is None
    assert "error=COALESCE(EXCLUDED.error, broker_orders.error)" in sql


@patch("librae.db.timescale_state.psycopg2.extras.execute_values")
@patch("librae.db.timescale_state.get_conn")
def test_a_nul_in_a_placement_error_cannot_freeze_the_order_row(mock_get_conn, mock_execute_values):
    """psycopg2 refuses a NUL in a string; venue error text is not ours to trust."""
    mock_get_conn.return_value.__enter__.return_value = MagicMock()
    tracked = TrackedOrder(
        request=OrderRequest(
            client_order_id="client-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        ),
        error="RuntimeError: bad\x00 payload",
    )

    TimescaleLiveStateStore().save(_state(), [tracked])

    assert mock_execute_values.call_args.args[2][0][-1] == "RuntimeError: bad payload"
    assert tracked.error == "RuntimeError: bad\x00 payload", "only the written value changes"


@patch("librae.db.timescale_state.psycopg2.extras.execute_values")
@patch("librae.db.timescale_state.get_conn")
def test_order_row_carries_reference_price_and_signed_shortfall(mock_get_conn, mock_execute_values):
    mock_get_conn.return_value.__enter__.return_value = MagicMock()
    tracked = TrackedOrder(
        request=OrderRequest(
            client_order_id="client-1",
            symbol="BTC/USDT",
            side="sell",
            quantity=2.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
            reference_price=100.0,
        ),
        placement_attempted=True,
        placement_attempted_at=datetime(2025, 1, 1, tzinfo=UTC),
        order_id="broker-1",
        status="filled",
        filled_quantity=2.0,
        filled_notional=198.0,
    )

    TimescaleLiveStateStore().save(_state(), [tracked])

    row = mock_execute_values.call_args.args[2][0]
    columns = mock_execute_values.call_args.args[1].split("VALUES")[0]
    names = [name.strip() for name in columns.split("(", 1)[1].rsplit(")", 1)[0].split(",")]
    assert row[names.index("slippage")] == pytest.approx(0.01)
    assert json.loads(row[names.index("request")])["reference_price"] == 100.0


@patch("librae.db.timescale_state.require_current_schema")
@patch("librae.db.timescale_state.get_conn")
def test_load_restores_json_checkpoint(mock_get_conn, _mock_schema):
    conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = conn
    conn.cursor.return_value.fetchone.return_value = (_state().to_dict(),)

    restored = TimescaleLiveStateStore().load("live:abc")

    assert restored == _state()


@patch("librae.db.timescale_state.get_conn")
def test_delete_reports_whether_a_checkpoint_existed(mock_get_conn):
    conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = conn
    cursor = conn.cursor.return_value
    cursor.rowcount = 1

    assert TimescaleLiveStateStore().delete("live:abc") is True
    cursor.execute.assert_called_once_with(
        "DELETE FROM execution_runtime_state WHERE state_key = %s", ("live:abc",)
    )

    cursor.rowcount = 0
    assert TimescaleLiveStateStore().delete("live:missing") is False


@patch("librae.db.timescale_state.get_pool")
def test_advisory_lease_holds_connection_until_release(mock_get_pool):
    pool = MagicMock()
    conn = MagicMock()
    conn.closed = 0
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (True,)
    mock_get_pool.return_value = pool
    pool.getconn.return_value = conn
    store = TimescaleLiveStateStore()

    assert store.acquire_lease("live:abc") is True
    pool.putconn.assert_not_called()

    store.release_lease("live:abc")

    assert cursor.execute.call_count == 2
    pool.putconn.assert_called_once_with(conn, close=False)
