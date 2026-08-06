"""Runtime checkpoint serialization and store contract tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState
from librae.live.executor import OrderRequest
from librae.live.state import (
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


@pytest.mark.parametrize(
    ("order_type", "limit_price", "expected"),
    [("market", None, "ioc"), ("limit", 99.0, "day")],
)
def test_order_request_resolves_default_time_in_force(order_type, limit_price, expected):
    request = OrderRequest(
        client_order_id="strategy-order-1",
        symbol="BTC/USDT",
        side="buy",
        quantity=2.0,
        order_type=order_type,
        limit_price=limit_price,
        submitted_at=datetime(2025, 1, 2, tzinfo=UTC),
    )
    assert request.time_in_force == expected


def test_order_request_preserves_explicit_time_in_force():
    request = OrderRequest(
        client_order_id="strategy-order-1",
        symbol="BTC/USDT",
        side="buy",
        quantity=2.0,
        order_type="limit",
        limit_price=99.0,
        submitted_at=datetime(2025, 1, 2, tzinfo=UTC),
        time_in_force="fok",
    )
    assert request.time_in_force == "fok"


def test_order_request_rejects_invalid_time_in_force():
    with pytest.raises(ValueError, match="time_in_force"):
        OrderRequest(
            client_order_id="strategy-order-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=2.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 2, tzinfo=UTC),
            time_in_force="gtd",
        )


def test_runtime_state_round_trip_preserves_restart_fields():
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        account_id="default",
        runtime_revision="revision-a",
        cash=800.0,
        positions={"BTC/USDT": _position()},
        last_prices={"BTC/USDT": 101.0},
        last_cycle_ts=datetime(2025, 1, 2, tzinfo=UTC),
        last_bar_ts={"BTC/USDT": datetime(2025, 1, 2, tzinfo=UTC)},
        last_funding_ts={"BTC/USDT": datetime(2025, 1, 2, tzinfo=UTC)},
        pending_decision=[OrderIntent(action="close", symbol="BTC/USDT")],
        active_orders=[_order()],
        equity_peak=1_050.0,
        prev_equity=1_002.0,
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


@pytest.mark.parametrize("runtime_revision", [None, "", "   "])
def test_live_runtime_state_requires_non_empty_runtime_revision(runtime_revision):
    with pytest.raises(ValueError, match="runtime_revision"):
        LiveRuntimeState(
            state_key="live:abc",
            run_id="run-1",
            config_hash="abc",
            mode="live",
            account_id="default",
            runtime_revision=runtime_revision,
            cash=1_000.0,
        )


def test_exact_future_order_identity_survives_checkpoint_round_trip():
    order = TrackedOrder(
        request=OrderRequest(
            client_order_id="strategy-es-1",
            symbol="ES_202609",
            venue_symbol="ES",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 2, tzinfo=UTC),
            security_type="FUT",
            exchange="CME",
            currency="USD",
            contract_month="202609",
        )
    )

    restored = TrackedOrder.from_dict(order.to_dict())

    assert restored.request.symbol == "ES_202609"
    assert restored.request.venue_symbol == "ES"
    assert restored.request.contract_month == "202609"


def test_live_rebalance_round_trip_and_memory_store_isolation():
    """pending_decision is always plain OrderIntents (validate_strategy_decision
    requires PortfolioWeights/grouped OrderIntents to be immediately
    executable, so they never sit as pending state); live_rebalance is the
    separate, still-PortfolioWeights-typed state for an in-flight leg-by-leg
    rebalance.
    """
    store = MemoryLiveStateStore()
    targets = PortfolioWeights(weights={"AAA": 0.6, "BBB": 0.4})
    state = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        account_id="default",
        cash=1_000.0,
        pending_decision=[OrderIntent(action="long", symbol="CCC", quantity=1.0)],
        live_rebalance=LiveRebalance(
            targets=targets,
            reference_prices={"AAA": 100.0, "BBB": 200.0},
            reference_volumes={"AAA": 1_000.0, "BBB": None},
            lagged_adv_by_symbol={"AAA": 10_000.0},
            decided_at=datetime(2025, 1, 1, tzinfo=UTC),
            next_sequence=1,
            filled_bar_quantity_by_symbol={"AAA": 5.0},
        ),
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
    assert second.pending_decision == state.pending_decision


def test_pending_order_intents_round_trip_through_to_dict():
    decision = [
        OrderIntent(action="long", symbol="AAA", quantity=1.0),
        OrderIntent(action="short", symbol="BBB", quantity=2.0),
    ]
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        account_id="default",
        cash=1_000.0,
        pending_decision=decision,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    )

    restored = LiveRuntimeState.from_dict(state.to_dict())

    assert restored.pending_decision == decision


def test_memory_store_lease_is_exclusive_until_release():
    store = MemoryLiveStateStore()

    assert store.acquire_lease("live:abc") is True
    assert store.acquire_lease("live:abc") is False
    store.release_lease("live:abc")
    assert store.acquire_lease("live:abc") is True


@pytest.mark.parametrize("version_delta", [-1, 1])
def test_runtime_state_rejects_non_current_schema(version_delta):
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        account_id="default",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    ).to_dict()
    raw["schema_version"] += version_delta

    with pytest.raises(ValueError, match=r"expected \d+, got \d+"):
        LiveRuntimeState.from_dict(raw)


def test_runtime_state_rejects_missing_required_fact_instead_of_defaulting():
    raw = LiveRuntimeState(
        state_key="sim:abc",
        run_id="run-1",
        config_hash="abc",
        mode="sim",
        account_id="default",
        cash=1_000.0,
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    ).to_dict()
    del raw["adv_filled_quantities"]

    with pytest.raises(KeyError, match="adv_filled_quantities"):
        LiveRuntimeState.from_dict(raw)


def test_live_runtime_state_rejects_missing_revision_in_current_schema():
    raw = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        account_id="default",
        runtime_revision="revision-a",
        cash=1_000.0,
    ).to_dict()
    del raw["runtime_revision"]

    with pytest.raises(KeyError, match="runtime_revision"):
        LiveRuntimeState.from_dict(raw)


def test_runtime_state_rejects_attempted_order_without_attempt_time():
    state = LiveRuntimeState(
        state_key="live:abc",
        run_id="run-1",
        config_hash="abc",
        mode="live",
        account_id="default",
        runtime_revision="revision-a",
        cash=1_000.0,
        active_orders=[_order()],
        equity_peak=1_000.0,
        prev_equity=1_000.0,
    )
    raw = state.to_dict()
    raw["active_orders"][0]["placement_attempted_at"] = None

    with pytest.raises(ValueError, match="missing placement_attempted_at"):
        LiveRuntimeState.from_dict(raw)
