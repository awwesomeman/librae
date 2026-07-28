"""Runtime checkpoint serialization and store contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.strategy import MultiLegOrder, OrderIntent, PortfolioTargets, PositionState
from librae.live.executor import OrderRequest
from librae.live.state import (
    LiveMultiLeg,
    LiveRebalance,
    LiveRuntimeState,
    MemoryLiveStateStore,
    TrackedOrder,
)


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
        pending_market_exit_reason="stop_loss",
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
        placement_attempted_at=datetime(2025, 1, 2, tzinfo=UTC),
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
        cash_by_account={"default": 800.0},
        positions={"BTC/USDT": _position()},
        last_prices={"BTC/USDT": 101.0},
        last_cycle_ts=datetime(2025, 1, 2, tzinfo=UTC),
        last_bar_ts={"BTC/USDT": datetime(2025, 1, 2, tzinfo=UTC)},
        pending_decision=[OrderIntent(action="close", symbol="BTC/USDT")],
        active_orders=[_order()],
        equity_peak_by_account={"default": 1_050.0},
        prev_equity_by_account={"default": 1_002.0},
        trade_count=4,
        event_sequence=7,
        period_index=8,
        status_period_count=2,
        halted=True,
        adv_session_labels={"BTC/USDT": "2025-01-02"},
        adv_filled_quantities={"BTC/USDT": 0.5},
    )

    restored = LiveRuntimeState.from_dict(state.to_dict())

    assert restored == state


def test_portfolio_targets_round_trip_and_memory_store_isolation():
    store = MemoryLiveStateStore()
    targets = PortfolioTargets(weights={"AAA": 0.6, "BBB": 0.4})
    state = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash_by_account={"default": 1_000.0},
        pending_decision=targets,
        live_rebalance=LiveRebalance(
            targets=targets,
            reference_prices={"AAA": 100.0, "BBB": 200.0},
            reference_volumes={"AAA": 1_000.0, "BBB": None},
            lagged_adv_by_symbol={"AAA": 10_000.0},
            decided_at=datetime(2025, 1, 1, tzinfo=UTC),
            next_sequence=1,
            filled_bar_quantity_by_symbol={"AAA": 5.0},
        ),
        equity_peak_by_account={"default": 1_000.0},
        prev_equity_by_account={"default": 1_000.0},
    )
    store.save(state)

    first = store.load(state.state_key)
    assert first is not None
    first.cash_by_account["default"] = 0.0
    second = store.load(state.state_key)

    assert second is not None
    assert second.cash_by_account["default"] == 1_000.0
    assert second.pending_decision == state.pending_decision


def test_multi_leg_order_round_trip():
    decision = MultiLegOrder(
        legs=(
            OrderIntent(action="long", symbol="BTC/USDT", quantity=1.0),
            OrderIntent(action="short", symbol="BTC-PERP", quantity=1.0),
        ),
        max_completion_seconds=2.5,
        reason="basis",
    )
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        cash_by_account={"default": 1_000.0},
        pending_decision=decision,
        live_multi_leg=LiveMultiLeg(
            order=decision,
            baseline_signed_quantities={"BTC/USDT": 1.0, "BTC-PERP": -1.0},
            reference_prices={"BTC/USDT": 100.0, "BTC-PERP": 101.0},
            reference_volumes={"BTC/USDT": 1_000.0, "BTC-PERP": 2_000.0},
            lagged_adv_by_symbol={"BTC/USDT": 10_000.0, "BTC-PERP": 20_000.0},
            decided_at=datetime(2025, 1, 1, tzinfo=UTC),
            next_leg_index=1,
            first_fill_at=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
        ),
        equity_peak_by_account={"default": 1_000.0},
        prev_equity_by_account={"default": 1_000.0},
    )

    restored = LiveRuntimeState.from_dict(state.to_dict())

    assert restored.pending_decision == decision
    assert restored.live_multi_leg is not None
    assert restored.live_multi_leg.baseline_signed_quantities == {
        "BTC/USDT": 1.0,
        "BTC-PERP": -1.0,
    }


@pytest.mark.parametrize(
    "legs",
    [
        (OrderIntent(action="long", symbol="AAA", quantity=1.0),),
        (
            OrderIntent(action="long", symbol="AAA"),
            OrderIntent(action="short", symbol="BBB", quantity=1.0),
        ),
        (
            OrderIntent(action="long", symbol="AAA", quantity=1.0),
            OrderIntent(action="short", symbol="AAA", quantity=1.0),
        ),
    ],
)
def test_multi_leg_order_rejects_unsafe_ambiguous_legs(legs):
    with pytest.raises(ValueError):
        MultiLegOrder(legs=legs)


def test_memory_store_lease_is_exclusive_until_release():
    store = MemoryLiveStateStore()

    assert store.acquire_lease("live:abc") is True
    assert store.acquire_lease("live:abc") is False
    store.release_lease("live:abc")
    assert store.acquire_lease("live:abc") is True


def test_runtime_state_rejects_v3_schema():
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash_by_account={"default": 1_000.0},
        equity_peak_by_account={"default": 1_000.0},
        prev_equity_by_account={"default": 1_000.0},
    ).to_dict()
    raw["schema_version"] = 3

    with pytest.raises(ValueError, match="unsupported live runtime-state schema"):
        LiveRuntimeState.from_dict(raw)


def test_runtime_state_rejects_missing_v6_fact_instead_of_defaulting():
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        cash_by_account={"default": 1_000.0},
        equity_peak_by_account={"default": 1_000.0},
        prev_equity_by_account={"default": 1_000.0},
    ).to_dict()
    del raw["adv_filled_quantities"]

    with pytest.raises(KeyError, match="adv_filled_quantities"):
        LiveRuntimeState.from_dict(raw)


def test_runtime_state_rejects_attempted_order_without_attempt_time():
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        cash_by_account={"default": 1_000.0},
        active_orders=[_order()],
        equity_peak_by_account={"default": 1_000.0},
        prev_equity_by_account={"default": 1_000.0},
    )
    raw = state.to_dict()
    raw["active_orders"][0]["placement_attempted_at"] = None

    with pytest.raises(ValueError, match="missing placement_attempted_at"):
        LiveRuntimeState.from_dict(raw)
