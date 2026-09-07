from __future__ import annotations

import pytest
from librae.core.executor import partition_pending_decision, validate_strategy_decision
from librae.core.strategy import OrderIntent


def _decision() -> list[OrderIntent]:
    return [
        OrderIntent(action="long", symbol="NEAR", quantity=1.0, group_id="roll"),
        OrderIntent(action="short", symbol="NEXT", quantity=1.0, group_id="roll"),
    ]


def _collapsed(doc: str | None) -> str:
    """Flatten a docstring to one line so assertions do not depend on where it
    happens to wrap, which reflows whenever the paragraph is edited."""
    return " ".join((doc or "").split())


def test_public_grouped_execution_contract_is_mode_specific() -> None:
    """The public API must not imply venue atomicity from group_id alone."""
    contract = _collapsed(OrderIntent.__doc__)

    assert "Backtest/sim" in contract
    assert "Live preflights" in contract
    assert "does not claim broker or cross-venue atomicity" in contract


def test_public_grouped_contract_states_who_can_act_on_each_failure() -> None:
    """A strategy author decides differently for a decision error than for a
    venue shortfall, so the split has to be on the public surface."""
    contract = _collapsed(OrderIntent.__doc__)

    assert "raises before anything is staged" in contract
    assert "group_unfillable" in contract


def test_public_grouped_contract_states_the_close_leg_quantity_rule() -> None:
    contract = _collapsed(OrderIntent.__doc__)

    assert "An entry leg requires an explicit quantity" in contract
    assert "a close leg may omit one to mean the whole position" in contract


def test_public_grouped_contract_states_group_identity_is_planned_not_settled() -> None:
    """Refusing a confirmed fill would leave the book behind the venue with
    nothing halted, so where the check runs is part of the contract."""
    contract = _collapsed(OrderIntent.__doc__)

    assert "refused when the order is planned" in contract
    assert "a confirmed fill is always booked" in contract


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


def test_grouped_close_leg_may_omit_quantity() -> None:
    """A close leg without a quantity means the whole position, which is
    deterministic given the book; an entry leg without one sizes from cash
    and stays rejected inside a group."""
    validate_strategy_decision(
        [
            OrderIntent(action="close", symbol="NEAR", group_id="exit"),
            OrderIntent(action="close", symbol="NEXT", group_id="exit"),
        ],
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
