"""TimescaleDB-backed sim/live runtime checkpoint and broker-order ledger."""

from __future__ import annotations

import json
from collections.abc import Sequence

import psycopg2.extras
from librae.live.state import LiveRuntimeState, TrackedOrder

from db import get_conn


class TimescaleLiveStateStore:
    """Persist one atomic runtime checkpoint plus append/update order facts."""

    def __init__(self, dsn: str | None = None) -> None:
        self._dsn = dsn

    def load(self, state_key: str) -> LiveRuntimeState | None:
        with get_conn(self._dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state FROM execution_runtime_state WHERE state_key = %s",
                (state_key,),
            )
            row = cur.fetchone()
            cur.close()
        if not row:
            return None
        raw = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        return LiveRuntimeState.from_dict(raw)

    def save(
        self,
        state: LiveRuntimeState,
        orders: Sequence[TrackedOrder] = (),
    ) -> None:
        """Atomically checkpoint portfolio state and upsert changed orders."""
        with get_conn(self._dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO execution_runtime_state
                       (state_key, run_id, config_hash, mode, state)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (state_key) DO UPDATE SET
                     run_id=EXCLUDED.run_id,
                     config_hash=EXCLUDED.config_hash,
                     mode=EXCLUDED.mode,
                     state=EXCLUDED.state,
                     updated_at=NOW()""",
                (
                    state.state_key,
                    state.run_id,
                    state.config_hash,
                    state.mode,
                    json.dumps(state.to_dict()),
                ),
            )
            if orders:
                rows = []
                for tracked in orders:
                    request = tracked.request
                    rows.append(
                        (
                            state.state_key,
                            request.client_order_id,
                            state.run_id,
                            tracked.order_id or None,
                            request.symbol,
                            request.side,
                            tracked.status,
                            tracked.placement_attempted,
                            tracked.placement_attempted_at,
                            request.quantity,
                            tracked.filled_quantity,
                            tracked.filled_notional,
                            tracked.commission,
                            tracked.slippage,
                            tracked.tax,
                            request.submitted_at,
                            tracked.executed_at,
                            json.dumps(tracked.to_dict()["request"]),
                        )
                    )
                psycopg2.extras.execute_values(
                    cur,
                    """INSERT INTO broker_orders
                           (state_key, client_order_id, run_id, broker_order_id,
                            symbol, side, status, placement_attempted,
                            placement_attempted_at,
                            requested_quantity,
                            filled_quantity, filled_notional, commission,
                            slippage, tax, submitted_at, executed_at, request)
                       VALUES %s
                       ON CONFLICT (state_key, client_order_id) DO UPDATE SET
                         broker_order_id=EXCLUDED.broker_order_id,
                         status=EXCLUDED.status,
                         placement_attempted=EXCLUDED.placement_attempted,
                         placement_attempted_at=EXCLUDED.placement_attempted_at,
                         filled_quantity=EXCLUDED.filled_quantity,
                         filled_notional=EXCLUDED.filled_notional,
                         commission=EXCLUDED.commission,
                         slippage=EXCLUDED.slippage,
                         tax=EXCLUDED.tax,
                         executed_at=EXCLUDED.executed_at,
                         updated_at=NOW()""",
                    rows,
                )
            cur.close()
