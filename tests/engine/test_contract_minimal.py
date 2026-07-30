from __future__ import annotations

from librae.core.executor import partition_pending_decision, validate_strategy_decision
from librae.core.strategy import MultiLegOrder, OrderIntent


def test_multi_leg_decision_waits_for_a_synchronous_market_event() -> None:
    decision = MultiLegOrder(
        legs=(
            OrderIntent(action="long", symbol="NEAR", quantity=1.0),
            OrderIntent(action="short", symbol="NEXT", quantity=1.0),
        )
    )
    validate_strategy_decision(decision, {"NEAR", "NEXT"}, primary_symbol="NEAR")

    ready, waiting = partition_pending_decision(
        decision,
        {"NEAR": {"close": 100.0}},
        {},
        primary_symbol="NEAR",
    )
    assert ready == []
    assert waiting == decision

    ready, waiting = partition_pending_decision(
        decision,
        {"NEAR": {"close": 100.0}, "NEXT": {"close": 101.0}},
        {},
        primary_symbol="NEAR",
    )
    assert ready == decision
    assert waiting == []
