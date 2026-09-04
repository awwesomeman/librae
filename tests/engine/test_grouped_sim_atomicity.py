"""Regression tests for fill-or-kill grouped intents in simulation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.core.cost_model import CostModel
from librae.core.executor import execute_pending_decision_and_stops
from librae.core.strategy import OrderIntent, PositionState

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


def test_untradable_leg_rejects_group_without_mutating_existing_positions() -> None:
    positions = {
        "A": _position("A"),
        "B": _position("B"),
    }
    decision = [
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
        OrderIntent(action="close", symbol="B", quantity=1.0, group_id="exit"),
    ]

    cash, result = _execute(
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
    assert cash == pytest.approx(0.0)
    assert result.events == []
    assert result.trades == []
    assert result.runtime_events[0].detail["group_id"] == "exit"
    assert result.runtime_events[0].detail["failed_reasons"] == ["missing_price"]


def test_partial_close_quantity_rejects_whole_group() -> None:
    positions = {
        "A": _position("A"),
        "B": _position("B"),
    }
    decision = [
        OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
        OrderIntent(action="close", symbol="B", quantity=2.0, group_id="exit"),
    ]

    cash, result = _execute(
        positions,
        0.0,
        decision,
        {"A": _bar(100.0), "B": _bar(100.0)},
    )

    assert set(positions) == {"A", "B"}
    assert positions["A"].quantity == pytest.approx(1.0)
    assert positions["B"].quantity == pytest.approx(1.0)
    assert cash == pytest.approx(0.0)
    assert result.events == []
    assert result.trades == []
    assert result.runtime_events[0].detail["failed_reasons"] == ["partial_fill"]
