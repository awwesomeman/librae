"""Regression tests for fill-or-kill grouped intents in simulation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.cost_model import CostModel
from librae.core.executor import execute_pending_decision_and_stops
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState

TS = datetime(2026, 1, 10, tzinfo=UTC)


def _bar(
    price: float,
    *,
    volume: float = 100.0,
    can_buy: bool = True,
    can_sell: bool = True,
) -> dict[str, float]:
    return {
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": volume,
        "can_buy": can_buy,
        "can_sell": can_sell,
    }


def _position(symbol: str, side: str = "long", quantity: float = 1.0) -> PositionState:
    return PositionState(
        symbol=symbol,
        side=side,
        entry_price=100.0,
        quantity=quantity,
        entry_at=TS,
        periods_held=0,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=100.0 * quantity,
    )


def _execute(
    positions: dict[str, PositionState],
    cash: float,
    decision: list[OrderIntent],
    bars: dict[str, dict[str, float]],
    *,
    max_bar_volume_participation_rate: float | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
):
    return execute_pending_decision_and_stops(
        TS,
        positions,
        cash,
        decision,
        bars,
        get_cost_model=lambda _symbol: CostModel.zero(),
        default_fill="open",
        primary_symbol="A",
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        get_previous_volume=lambda symbol: bars.get(symbol, {}).get("volume"),
        used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
    )


def test_cash_shortfall_rejects_whole_group_but_ungrouped_intent_still_fills() -> None:
    positions: dict[str, PositionState] = {}
    decision = [
        OrderIntent(action="long", symbol="A", quantity=1.0, group_id="pair"),
        OrderIntent(action="long", symbol="B", quantity=1.0, group_id="pair"),
        OrderIntent(action="long", symbol="C", quantity=1.0),
    ]

    cash, result = _execute(
        positions,
        150.0,
        decision,
        {"A": _bar(100.0), "B": _bar(100.0), "C": _bar(50.0)},
    )

    assert set(positions) == {"C"}
    assert cash == pytest.approx(100.0)
    assert [event.symbol for event in result.events] == ["C"]
    assert result.trades == []
    assert result.runtime_events[0].detail == {
        "reason": "group_unfillable",
        "group_id": "pair",
        "symbols": ["A", "B"],
        "failed_reasons": ["insufficient_cash"],
    }


def test_partial_volume_rejects_one_group_without_blocking_another_group() -> None:
    positions: dict[str, PositionState] = {}
    adv_usage: dict[str, float] = {}
    decision = [
        OrderIntent(action="long", symbol="A", quantity=10.0, group_id="thin"),
        OrderIntent(action="short", symbol="B", quantity=10.0, group_id="thin"),
        OrderIntent(action="long", symbol="C", quantity=1.0, group_id="liquid"),
        OrderIntent(action="short", symbol="D", quantity=1.0, group_id="liquid"),
    ]

    cash, result = _execute(
        positions,
        10_000.0,
        decision,
        {
            "A": _bar(100.0, volume=100.0),
            "B": _bar(100.0, volume=5.0),
            "C": _bar(50.0, volume=100.0),
            "D": _bar(50.0, volume=100.0),
        },
        max_bar_volume_participation_rate=0.1,
        used_adv_quantity_by_symbol=adv_usage,
    )

    assert set(positions) == {"C", "D"}
    assert positions["C"].quantity == pytest.approx(1.0)
    assert positions["D"].quantity == pytest.approx(1.0)
    assert cash == pytest.approx(9_900.0)
    assert {event.group_id for event in result.events} == {"liquid"}
    assert adv_usage == {"C": pytest.approx(1.0), "D": pytest.approx(1.0)}
    assert result.runtime_events[0].detail["group_id"] == "thin"
    assert result.runtime_events[0].detail["failed_reasons"] == ["partial_fill"]


def test_untradable_leg_raises_before_mutating_existing_positions() -> None:
    """A leg that lost its price cannot fill, and a group that cannot fill
    every leg fails loudly rather than as a skip event, per
    docs/decisions/2026-08-05-grouped-decisions-no-engine-side-waiting.md."""
    positions = {
        "A": _position("A"),
        "B": _position("B"),
    }
    decision = [
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
        OrderIntent(action="close", symbol="B", quantity=1.0, group_id="exit"),
    ]

    with pytest.raises(ValueError, match="lost pricing"):
        _execute(
            positions,
            0.0,
            decision,
            {
                "A": _bar(100.0),
                "B": _bar(100.0, can_sell=False),
            },
        )

    assert set(positions) == {"A", "B"}
    assert positions["A"].quantity == pytest.approx(1.0)
    assert positions["B"].quantity == pytest.approx(1.0)


def test_close_leg_requesting_more_than_held_raises_before_mutation() -> None:
    """The stated quantity no longer describes the book. Silently skipping
    would leave the pair open and un-exitable by this group id on every later
    bar; a close leg that means "all of it" omits the quantity instead."""
    positions = {
        "A": _position("A"),
        "B": _position("B"),
    }
    decision = [
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
        OrderIntent(action="close", symbol="B", quantity=2.0, group_id="exit"),
    ]

    with pytest.raises(ValueError, match="B"):
        _execute(positions, 0.0, decision, {"A": _bar(100.0), "B": _bar(100.0)})

    assert set(positions) == {"A", "B"}
    assert positions["A"].quantity == pytest.approx(1.0)
    assert positions["B"].quantity == pytest.approx(1.0)


def test_close_leg_on_a_flat_symbol_raises_before_mutation() -> None:
    positions = {"A": _position("A")}
    decision = [
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
        OrderIntent(action="close", symbol="B", quantity=1.0, group_id="exit"),
    ]

    with pytest.raises(ValueError, match="B"):
        _execute(positions, 0.0, decision, {"A": _bar(100.0), "B": _bar(100.0)})

    assert positions["A"].quantity == pytest.approx(1.0)


def test_close_legs_without_quantity_close_everything() -> None:
    """The only way to exit a pair whose legs shrank underneath the strategy
    (a volume-capped stop reduced one) is to say "all of it"."""
    positions = {"A": _position("A", quantity=10.0), "B": _position("B", quantity=8.0)}
    decision = [
        OrderIntent(action="close", symbol="A", group_id="exit"),
        OrderIntent(action="close", symbol="B", group_id="exit"),
    ]

    cash, result = _execute(positions, 0.0, decision, {"A": _bar(100.0), "B": _bar(100.0)})

    assert positions == {}
    assert cash == pytest.approx(1_800.0)
    assert [event.event_type for event in result.events] == ["close", "close"]
    assert result.runtime_events == []


def test_group_preflight_raises_before_any_ungrouped_intent_fills() -> None:
    """Ungrouped units commit straight into the caller's book, so a group
    that will raise must be found before the first unit executes -- otherwise
    the book moves while the cash delta is discarded with the exception."""
    positions: dict[str, PositionState] = {}
    decision = [
        OrderIntent(action="long", symbol="C", quantity=1.0),
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
    ]

    with pytest.raises(ValueError, match="A"):
        _execute(
            positions,
            10_000.0,
            decision,
            {"A": _bar(100.0), "C": _bar(50.0)},
        )

    assert positions == {}


@pytest.mark.parametrize(
    ("position_group", "intent_group"),
    [
        ("original", "different"),
        ("original", None),
        (None, "new-group"),
    ],
)
def test_scale_in_rejects_cross_group_identity(
    position_group: str | None,
    intent_group: str | None,
) -> None:
    position = _position("A")
    position.group_id = position_group
    positions = {"A": position}

    with pytest.raises(ValueError, match="across group identities"):
        _execute(
            positions,
            1_000.0,
            [
                OrderIntent(
                    action="long",
                    symbol="A",
                    quantity=1.0,
                    group_id=intent_group,
                )
            ],
            {"A": _bar(100.0)},
        )

    assert positions == {"A": position}
    assert position.quantity == pytest.approx(1.0)
    assert position.group_id == position_group


def test_cross_group_scale_in_leaves_an_earlier_ungrouped_intent_unfilled() -> None:
    """Ungrouped units commit straight into the book, so the violation must
    be found before the first unit runs -- not when the group is staged."""
    position = _position("A")
    position.group_id = "original"
    positions = {"A": position}
    decision = [
        OrderIntent(action="short", symbol="B", quantity=1.0),
        OrderIntent(action="long", symbol="A", quantity=1.0, group_id="replacement"),
    ]

    with pytest.raises(ValueError, match="across group identities"):
        _execute(
            positions,
            1_000.0,
            decision,
            {"A": _bar(100.0), "B": _bar(100.0)},
        )

    assert positions == {"A": position}
    assert position.quantity == pytest.approx(1.0)
    assert "B" not in positions


def test_portfolio_weights_refuses_to_scale_a_grouped_position_before_reducing() -> None:
    """A whole-book target is ungrouped by nature; adding to a position a
    group opened would break its identity, and the refusal lands before the
    reductions the same target would otherwise execute first."""
    grouped = _position("A", quantity=1.0)
    grouped.group_id = "pair"
    positions = {"A": grouped, "B": _position("B", quantity=4.0)}

    with pytest.raises(ValueError, match="A"):
        _execute(
            positions,
            0.0,
            PortfolioWeights(weights={"A": 0.9, "B": 0.1}),
            {"A": _bar(100.0), "B": _bar(100.0)},
        )

    assert positions["A"].quantity == pytest.approx(1.0)
    assert positions["B"].quantity == pytest.approx(4.0)


def test_portfolio_weights_may_reduce_a_grouped_position() -> None:
    grouped = _position("A", quantity=4.0)
    grouped.group_id = "pair"
    positions = {"A": grouped}

    _cash, result = _execute(
        positions,
        0.0,
        PortfolioWeights(weights={"A": 0.5}),
        {"A": _bar(100.0)},
    )

    assert positions["A"].quantity == pytest.approx(2.0)
    assert positions["A"].group_id == "pair"
    assert result.events[0].event_type == "reduce"
    assert result.events[0].group_id == "pair"
