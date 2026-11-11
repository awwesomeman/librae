"""Timescale runtime-state adapter tests without a real database."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.errors
import pytest
from librae.db.timescale_state import TimescaleLiveStateStore
from librae.live.execution_identity import ExecutionIdentity
from librae.live.executor import OrderRequest
from librae.live.state import LiveRuntimeState, StateStoreUnavailableError, TrackedOrder


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
            reference_price=100.0,
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
            reference_price=100.0,
        ),
        error="RuntimeError: bad\x00 payload",
    )

    TimescaleLiveStateStore().save(_state(), [tracked])

    assert mock_execute_values.call_args.args[2][0][-1] == "RuntimeError: bad payload"
    assert tracked.error == "RuntimeError: bad\x00 payload", "only the written value changes"


@patch("librae.db.timescale_state.psycopg2.extras.execute_values")
@patch("librae.db.timescale_state.get_conn")
def test_a_lone_surrogate_in_a_placement_error_cannot_freeze_the_order_row(
    mock_get_conn, mock_execute_values
):
    """psycopg2 cannot encode a lone surrogate, so the row would be refused."""
    mock_get_conn.return_value.__enter__.return_value = MagicMock()
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
        error="RuntimeError: bad \udc80 payload",
    )

    TimescaleLiveStateStore().save(_state(), [tracked])

    written = mock_execute_values.call_args.args[2][0][-1]
    assert written == "RuntimeError: bad \\udc80 payload"


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


@patch("librae.db.timescale_state._connect_unpooled")
def test_advisory_lease_holds_its_own_connection_until_release(mock_connect):
    conn = MagicMock()
    cursor = conn.cursor.return_value
    cursor.fetchone.return_value = (True,)
    mock_connect.return_value = conn
    store = TimescaleLiveStateStore()

    assert store.acquire_lease("live:abc") is True
    conn.close.assert_not_called()

    store.release_lease("live:abc")

    assert cursor.execute.call_count == 2
    conn.close.assert_called_once()


@patch("librae.db.timescale_state._connect_unpooled")
def test_a_lease_held_elsewhere_closes_its_connection(mock_connect):
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = (False,)
    mock_connect.return_value = conn

    assert TimescaleLiveStateStore().acquire_lease("live:abc") is False
    conn.close.assert_called_once()


@pytest.mark.parametrize(
    "call",
    [
        lambda store: store.save(_state()),
        lambda store: store.load("live:abc"),
        lambda store: store.delete("live:abc"),
    ],
    ids=["save", "load", "delete"],
)
def test_every_checkpoint_statement_is_bounded(call):
    """The engine waits on them in its loop, halts and order lookups included."""
    import librae.db as db

    bounded: list[bool] = []

    def checkout(_dsn):
        bounded.append(db._bounded.get())
        checkout_cm = MagicMock()
        cursor = checkout_cm.__enter__.return_value.cursor.return_value
        cursor.fetchone.return_value = None
        cursor.rowcount = 0
        return checkout_cm

    with (
        patch("librae.db.timescale_state.get_conn", side_effect=checkout),
        patch("librae.db.timescale_state.require_current_schema"),
    ):
        call(TimescaleLiveStateStore())

    assert bounded == [True]


@pytest.mark.parametrize(
    "error",
    [
        psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"),
        psycopg2.OperationalError("could not receive data from server: Connection timed out"),
        psycopg2.InterfaceError("connection already closed"),
    ],
    ids=["statement_timeout", "lost_connection", "closed_connection"],
)
@patch("librae.db.timescale_state.get_conn")
def test_a_save_the_server_never_judged_is_unavailable(mock_get_conn, error):
    """A timeout, e.g. behind a lock on an order row, is no refusal of that row."""
    cursor = mock_get_conn.return_value.__enter__.return_value.cursor.return_value
    cursor.execute.side_effect = error

    with pytest.raises(StateStoreUnavailableError, match=type(error).__name__) as raised:
        TimescaleLiveStateStore().save(_state())

    assert raised.value.__cause__ is error


@patch("librae.db.timescale_state.get_conn")
def test_a_save_the_server_refused_keeps_its_error(mock_get_conn):
    cursor = mock_get_conn.return_value.__enter__.return_value.cursor.return_value
    cursor.execute.side_effect = psycopg2.errors.NumericValueOutOfRange("numeric field overflow")

    with pytest.raises(psycopg2.errors.NumericValueOutOfRange):
        TimescaleLiveStateStore().save(_state())
