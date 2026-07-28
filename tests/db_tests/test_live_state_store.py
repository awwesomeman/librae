"""Timescale runtime-state adapter tests without a real database."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from db.timescale_state import TimescaleLiveStateStore
from librae.live.executor import OrderRequest
from librae.live.state import LiveRuntimeState, TrackedOrder


def _state() -> LiveRuntimeState:
    return LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    )


@patch("db.timescale_state.psycopg2.extras.execute_values")
@patch("db.timescale_state.get_conn")
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


@patch("db.timescale_state.get_conn")
def test_load_restores_json_checkpoint(mock_get_conn):
    conn = MagicMock()
    mock_get_conn.return_value.__enter__.return_value = conn
    conn.cursor.return_value.fetchone.return_value = (_state().to_dict(),)

    restored = TimescaleLiveStateStore().load("live:abc")

    assert restored == _state()
