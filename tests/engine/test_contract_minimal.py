from __future__ import annotations

import pytest
from librae.core.executor import partition_pending_decision, validate_strategy_decision
from librae.core.strategy import OrderIntent


def _decision() -> list[OrderIntent]:
    return [
        OrderIntent(action="long", symbol="NEAR", quantity=1.0, group_id="roll"),
        OrderIntent(action="short", symbol="NEXT", quantity=1.0, group_id="roll"),
    ]


def test_grouped_decision_rejected_when_a_required_symbol_has_no_bar() -> None:
    decision = _decision()

    with pytest.raises(ValueError, match="NEXT"):
        validate_strategy_decision(
            decision,
            {"NEAR", "NEXT"},
            primary_symbol="NEAR",
            bars={"NEAR": {"close": 100.0}},
            positions={},
        )


def test_grouped_decision_accepted_once_every_symbol_has_a_bar() -> None:
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


@pytest.mark.parametrize(
    "decision",
    [
        [
            OrderIntent(action="long", symbol="NEAR", group_id="roll"),
            OrderIntent(action="short", symbol="NEXT", quantity=1.0, group_id="roll"),
        ],
        [
            OrderIntent(action="long", symbol="NEAR", quantity=1.0, group_id="roll"),
            OrderIntent(action="short", symbol="NEAR", quantity=1.0, group_id="roll"),
        ],
    ],
)
def test_grouped_decision_rejects_unsafe_ambiguous_legs(decision) -> None:
    with pytest.raises(ValueError):
        validate_strategy_decision(
            decision,
            {"NEAR", "NEXT"},
            primary_symbol="NEAR",
            bars={"NEAR": {"close": 100.0}, "NEXT": {"close": 101.0}},
            positions={},
        )


@pytest.mark.parametrize("invalid", ["gtd", "GTC", "", 1])
def test_order_intent_rejects_invalid_time_in_force(invalid) -> None:
    with pytest.raises(ValueError, match="time_in_force"):
        OrderIntent(action="long", symbol="AAA", time_in_force=invalid)


@pytest.mark.parametrize("valid", ["day", "gtc", "ioc", "fok"])
def test_order_intent_accepts_valid_time_in_force(valid) -> None:
    intent = OrderIntent(action="long", symbol="AAA", time_in_force=valid)
    assert intent.time_in_force == valid
