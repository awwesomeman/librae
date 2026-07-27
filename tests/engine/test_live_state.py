"""Runtime checkpoint serialization and store contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.strategy import Action, PositionState, RebalanceTargets
from librae.live.executor import OrderRequest
from librae.live.state import LiveRuntimeState, MemoryLiveStateStore, TrackedOrder


def _position() -> PositionState:
    return PositionState(
        symbol="BTC/USDT",
        side="long",
        entry_price=100.0,
        quantity=2.0,
        entry_at=datetime(2025, 1, 1, tzinfo=UTC),
        periods_held=3,
        entry_commission=0.2,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=200.0,
    )


def _order() -> TrackedOrder:
    return TrackedOrder(
        request=OrderRequest(
            client_order_id="strategy-order-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.0,
            order_type="limit",
            limit_price=99.0,
            submitted_at=datetime(2025, 1, 2, tzinfo=UTC),
        ),
        placement_attempted=True,
        order_id="broker-1",
        status="partial",
        filled_quantity=0.5,
        filled_notional=49.5,
        commission=0.1,
        executed_at=datetime(2025, 1, 2, 0, 1, tzinfo=UTC),
    )


def test_runtime_state_round_trip_preserves_restart_fields():
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        cash=800.0,
        positions={"BTC/USDT": _position()},
        last_prices={"BTC/USDT": 101.0},
        last_cycle_ts=datetime(2025, 1, 2, tzinfo=UTC),
        last_bar_ts={"BTC/USDT": datetime(2025, 1, 2, tzinfo=UTC)},
        pending_intent=[Action(type="close", symbol="BTC/USDT")],
        active_orders=[_order()],
        equity_peak=1_050.0,
        prev_equity=1_002.0,
        trade_count=4,
        event_sequence=7,
        period_index=8,
        status_period_count=2,
        halted=True,
    )

    restored = LiveRuntimeState.from_dict(state.to_dict())

    assert restored == state


def test_rebalance_intent_round_trip_and_memory_store_isolation():
    store = MemoryLiveStateStore()
    state = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash=1_000.0,
        pending_intent=RebalanceTargets(weights={"AAA": 0.6, "BBB": 0.4}),
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    )
    store.save(state)

    first = store.load(state.state_key)
    assert first is not None
    first.cash = 0.0
    second = store.load(state.state_key)

    assert second is not None
    assert second.cash == 1_000.0
    assert second.pending_intent == state.pending_intent


def test_runtime_state_rejects_pre_watermark_schema():
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    ).to_dict()
    raw["schema_version"] = 1

    with pytest.raises(ValueError, match="unsupported live runtime-state schema"):
        LiveRuntimeState.from_dict(raw)


def test_runtime_state_rejects_missing_v2_fact_instead_of_defaulting():
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    ).to_dict()
    del raw["last_prices"]

    with pytest.raises(KeyError, match="last_prices"):
        LiveRuntimeState.from_dict(raw)
