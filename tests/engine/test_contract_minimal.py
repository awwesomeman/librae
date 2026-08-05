from __future__ import annotations

import pytest
from librae.core.executor import partition_pending_decision, validate_strategy_decision
from librae.core.strategy import MultiLegOrder, OrderIntent


def _decision() -> MultiLegOrder:
    return MultiLegOrder(
        legs=(
            OrderIntent(action="long", symbol="NEAR", quantity=1.0),
            OrderIntent(action="short", symbol="NEXT", quantity=1.0),
        )
    )


def test_multi_leg_decision_rejected_when_a_required_symbol_has_no_bar() -> None:
    decision = _decision()

    with pytest.raises(ValueError, match="NEXT"):
        validate_strategy_decision(
            decision,
            {"NEAR", "NEXT"},
            primary_symbol="NEAR",
            bars={"NEAR": {"close": 100.0}},
            positions={},
        )


def test_multi_leg_decision_accepted_once_every_symbol_has_a_bar() -> None:
    decision = _decision()

    validate_strategy_decision(
        decision,
        {"NEAR", "NEXT"},
        primary_symbol="NEAR",
        bars={"NEAR": {"close": 100.0}, "NEXT": {"close": 101.0}},
        positions={},
    )

    ready, waiting = partition_pending_decision(
        decision,
        {"NEAR": {"close": 100.0}, "NEXT": {"close": 101.0}},
        {},
        primary_symbol="NEAR",
    )
    assert ready == decision
    assert waiting == []
