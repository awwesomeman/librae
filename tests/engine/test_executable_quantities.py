"""Executable quantity contracts shared by backtest, simulation, and live planning."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.config.symbols import SymbolInfo, get_symbol, resolve_symbol
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_FORCE_CLOSE,
    check_stop_targets,
    execute_order_intents,
    execute_pending_decision_and_stops,
    execute_portfolio_weights,
    liquidate_all,
)
from librae.core.run_config import AccountConfig, RunConfig
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState

TS = datetime(2026, 9, 8, tzinfo=UTC)


def _instrument(
    symbol: str,
    *,
    quantity_step: float,
    min_quantity: float | None = None,
    price_increment: float | None = None,
    min_notional: float | None = None,
) -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol,
        market="test",
        data_source="test",
        instrument_type="spot",
        multiplier=1.0,
        data_adapter="test",
        venue_symbol=symbol,
        currency="USD",
        quantity_step=quantity_step,
        min_quantity=min_quantity,
        price_increment=price_increment,
        min_notional=min_notional,
    )


def _normalizer(instruments: dict[str, SymbolInfo]):
    return lambda symbol, quantity: instruments[symbol].normalize_quantity(quantity)


def _execute(
    intents: list[OrderIntent],
    instruments: dict[str, SymbolInfo],
    *,
    atomic_groups: bool = False,
    max_position_notional: float | None = None,
    max_volume_quantity: float | None = None,
):
    return execute_order_intents(
        intents,
        {},
        100_000.0,
        TS,
        get_price=lambda _symbol, _intent: 100.0,
        get_cost_model=lambda _symbol: CostModel.zero(),
        primary_symbol=next(iter(instruments)),
        atomic_groups=atomic_groups,
        max_position_notional=max_position_notional,
        max_bar_volume_participation_rate=(1.0 if max_volume_quantity is not None else None),
        get_volume=(
            (lambda _symbol: max_volume_quantity) if max_volume_quantity is not None else None
        ),
        get_executable_quantity=_normalizer(instruments),
        validate_intent_prices=lambda symbol, intent: instruments[symbol].validate_order_prices(
            intent
        ),
        get_min_notional=lambda symbol: instruments[symbol].min_notional,
    )


def test_symbol_quantity_uses_decimal_rounding_toward_zero() -> None:
    instrument = _instrument("COIN", quantity_step=0.001, min_quantity=0.005)

    assert instrument.normalize_quantity(1.2349) == pytest.approx(1.234)
    assert instrument.normalize_quantity(0.0049) == 0.0


def test_builtin_futures_and_equity_declare_whole_units() -> None:
    for symbol in ("TXFR1", "MXFR1", "TMFR1", "MU"):
        instrument = get_symbol(symbol)
        assert instrument.quantity_step == 1.0
        assert instrument.min_quantity == 1.0


def test_instrument_override_supplies_crypto_step_and_minimum() -> None:
    config = RunConfig(
        strategy_name="quantity",
        symbols=["COIN"],
        timeframe="H1",
        market="crypto",
        data_source="test",
        account=AccountConfig(currency="USD", initial_cash=10_000.0),
        mode="backtest",
        instrument_overrides={
            "COIN": {
                "data_adapter": "test",
                "instrument_type": "spot",
                "currency": "USD",
                "quantity_step": 0.01,
                "min_quantity": 0.05,
            }
        },
        symbol_cost_overrides={"COIN": {"multiplier": 1.0}},
    )

    instrument = resolve_symbol(config, "COIN")

    assert instrument.normalize_quantity(0.079) == pytest.approx(0.07)
    assert instrument.normalize_quantity(0.049) == 0.0


def test_explicit_quantity_is_normalized_before_fill() -> None:
    instruments = {"FUT": _instrument("FUT", quantity_step=1.0, min_quantity=1.0)}

    result = _execute(
        [OrderIntent(action="long", symbol="FUT", quantity=2.9)],
        instruments,
    )

    assert result.events[0].fill_quantity == 2.0


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [("position", 2.0), ("volume", 2.0)],
)
def test_constraint_created_fraction_is_normalized(
    constraint: str,
    expected: float,
) -> None:
    instruments = {"FUT": _instrument("FUT", quantity_step=1.0, min_quantity=1.0)}
    kwargs = (
        {"max_position_notional": 290.0}
        if constraint == "position"
        else {"max_volume_quantity": 2.9}
    )

    result = _execute(
        [OrderIntent(action="long", symbol="FUT", quantity=10.0)],
        instruments,
        **kwargs,
    )

    assert result.events[0].fill_quantity == expected


def test_quantity_below_minimum_is_audited_as_skipped() -> None:
    instruments = {"COIN": _instrument("COIN", quantity_step=0.01, min_quantity=0.05)}

    result = _execute(
        [OrderIntent(action="long", symbol="COIN", quantity=0.049)],
        instruments,
    )

    assert result.events == []
    assert result.runtime_events[0].detail == {
        "reason": "quantity_not_executable",
        "requested_quantity": 0.049,
        "executable_quantity": 0.0,
    }


@pytest.mark.parametrize(("quantity", "fills"), [(0.99, False), (1.0, True)])
def test_minimum_notional_is_enforced_after_quantity_normalization(
    quantity: float,
    fills: bool,
) -> None:
    instruments = {
        "COIN": _instrument(
            "COIN",
            quantity_step=0.01,
            min_notional=100.0,
        )
    }

    result = _execute(
        [OrderIntent(action="long", symbol="COIN", quantity=quantity)],
        instruments,
    )

    assert bool(result.events) is fills
    if not fills:
        assert result.runtime_events[0].detail["reason"] == "notional_below_minimum"


def test_cost_model_slippage_cannot_satisfy_minimum_notional() -> None:
    instrument = _instrument("COIN", quantity_step=0.01, min_notional=100.0)

    result = execute_order_intents(
        [OrderIntent(action="long", symbol="COIN", quantity=1.0)],
        {},
        100_000.0,
        TS,
        get_price=lambda _symbol, _intent: 90.0,
        get_cost_model=lambda _symbol: CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=2.0,
            tick_size=10.0,
            tax_rate=0.0,
        ),
        primary_symbol="COIN",
        get_min_notional=lambda _symbol: instrument.min_notional,
    )

    assert result.events == []
    assert result.runtime_events[0].detail["reason"] == "notional_below_minimum"


def test_sizing_cap_cannot_leave_an_entry_below_minimum_notional() -> None:
    instruments = {"COIN": _instrument("COIN", quantity_step=0.01, min_notional=100.0)}

    result = _execute(
        [OrderIntent(action="long", symbol="COIN", quantity=2.0)],
        instruments,
        max_position_notional=99.0,
    )

    assert result.events == []
    assert result.runtime_events[0].detail["reason"] == "notional_below_minimum"


def test_portfolio_weight_entry_respects_minimum_notional() -> None:
    instruments = {"COIN": _instrument("COIN", quantity_step=0.01, min_notional=150.0)}

    result = execute_portfolio_weights(
        PortfolioWeights({"COIN": 0.1}),
        {},
        1_000.0,
        TS,
        get_price=lambda _symbol, _intent: 100.0,
        get_cost_model=lambda _symbol: CostModel.zero(),
        primary_symbol="COIN",
        get_executable_quantity=_normalizer(instruments),
        get_min_notional=lambda symbol: instruments[symbol].min_notional,
    )

    assert result.events == []
    assert result.runtime_events[0].detail["reason"] == "notional_below_minimum"


def test_defer_all_preflights_minimum_notional_before_selling() -> None:
    instruments = {
        "A": _instrument("A", quantity_step=1.0),
        "B": _instrument("B", quantity_step=1.0, min_notional=150.0),
    }
    positions = {
        "A": PositionState(
            symbol="A",
            side="long",
            entry_price=100.0,
            quantity=10.0,
            entry_at=TS,
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=1_000.0,
        )
    }
    bars = {
        symbol: {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 100.0,
        }
        for symbol in instruments
    }

    updated_cash, result = execute_pending_decision_and_stops(
        TS,
        positions,
        0.0,
        PortfolioWeights({"B": 0.1}),
        bars,
        get_cost_model=lambda _symbol: CostModel.zero(),
        default_fill="open",
        primary_symbol="A",
        rebalance_residual_policy="defer_all",
        get_executable_quantity=_normalizer(instruments),
        get_min_notional=lambda symbol: instruments[symbol].min_notional,
    )

    assert updated_cash == 0.0
    assert positions["A"].quantity == 10.0
    assert "B" not in positions
    assert result.events == []
    assert result.trades == []
    assert result.pending_rebalance is not None
    remaining_orders = {
        (order.phase, order.intent.symbol, order.remaining_quantity)
        for order in result.pending_rebalance.orders
    }
    assert remaining_orders == {
        ("reduction", "A", 10.0),
        ("addition", "B", 1.0),
    }
    residuals = [
        event
        for event in result.runtime_events
        if event.detail.get("reason") == "rebalance_residual"
    ]
    assert [(event.symbol, event.detail["filled_quantity"]) for event in residuals] == [
        ("A", 0.0),
        ("B", 0.0),
    ]
    assert all(event.detail["blocked_symbols"] == ["B"] for event in residuals)
    b_residual = next(event for event in residuals if event.symbol == "B")
    assert any(
        related.get("reason") == "notional_below_minimum"
        for related in b_residual.detail["related_events"]
    )


def test_minimum_notional_never_blocks_an_exposure_reducing_close() -> None:
    positions = {
        "COIN": PositionState(
            symbol="COIN",
            side="long",
            entry_price=100.0,
            quantity=0.5,
            entry_at=TS,
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=50.0,
        )
    }

    result = execute_order_intents(
        [OrderIntent(action="close", symbol="COIN")],
        positions,
        1_000.0,
        TS,
        get_price=lambda _symbol, _intent: 90.0,
        get_cost_model=lambda _symbol: CostModel.zero(),
        primary_symbol="COIN",
        get_min_notional=lambda _symbol: 100.0,
    )

    assert positions == {}
    assert result.events[0].event_type == "close"


def test_minimum_notional_rejects_an_entire_atomic_group() -> None:
    instruments = {
        "A": _instrument("A", quantity_step=1.0, min_notional=50.0),
        "B": _instrument("B", quantity_step=1.0, min_notional=150.0),
    }
    positions: dict[str, PositionState] = {}

    result = execute_order_intents(
        [
            OrderIntent(action="long", symbol="A", quantity=1.0, group_id="spread"),
            OrderIntent(action="short", symbol="B", quantity=1.0, group_id="spread"),
        ],
        positions,
        100_000.0,
        TS,
        get_price=lambda _symbol, _intent: 100.0,
        get_cost_model=lambda _symbol: CostModel.zero(),
        primary_symbol="A",
        atomic_groups=True,
        get_executable_quantity=_normalizer(instruments),
        get_min_notional=lambda symbol: instruments[symbol].min_notional,
    )

    assert positions == {}
    assert result.events == []
    assert result.runtime_events[0].detail["reason"] == "group_unfillable"
    assert result.runtime_events[0].detail["failed_reasons"] == ["notional_below_minimum"]


def test_invalid_price_grid_rejects_group_before_any_position_mutation() -> None:
    instruments = {
        "A": _instrument("A", quantity_step=1.0, price_increment=0.25),
        "B": _instrument("B", quantity_step=1.0, price_increment=0.25),
    }
    positions: dict[str, PositionState] = {}

    with pytest.raises(ValueError, match=r"limit_price.*price_increment"):
        execute_order_intents(
            [
                OrderIntent(
                    action="long",
                    symbol="A",
                    quantity=1.0,
                    limit_price=100.25,
                    group_id="spread",
                ),
                OrderIntent(
                    action="short",
                    symbol="B",
                    quantity=1.0,
                    limit_price=100.125,
                    group_id="spread",
                ),
            ],
            positions,
            100_000.0,
            TS,
            get_price=lambda _symbol, intent: intent.limit_price,
            get_cost_model=lambda _symbol: CostModel.zero(),
            primary_symbol="A",
            atomic_groups=True,
            get_executable_quantity=_normalizer(instruments),
            validate_intent_prices=lambda symbol, intent: instruments[symbol].validate_order_prices(
                intent
            ),
        )

    assert positions == {}


def test_group_rejects_quantity_normalization_that_changes_leg_ratios() -> None:
    instruments = {
        "A": _instrument("A", quantity_step=1.0),
        "B": _instrument("B", quantity_step=2.0),
    }
    positions = {}

    with pytest.raises(ValueError, match="changes relative leg ratios"):
        execute_order_intents(
            [
                OrderIntent(action="long", symbol="A", quantity=2.7, group_id="spread"),
                OrderIntent(action="short", symbol="B", quantity=5.0, group_id="spread"),
            ],
            positions,
            100_000.0,
            TS,
            get_price=lambda _symbol, _intent: 100.0,
            get_cost_model=lambda _symbol: CostModel.zero(),
            primary_symbol="A",
            atomic_groups=True,
            get_executable_quantity=_normalizer(instruments),
        )

    assert positions == {}


def test_group_accepts_quantity_normalization_that_preserves_leg_ratios() -> None:
    instruments = {
        "A": _instrument("A", quantity_step=1.0),
        "B": _instrument("B", quantity_step=2.0),
    }

    result = _execute(
        [
            OrderIntent(action="long", symbol="A", quantity=2.7, group_id="spread"),
            OrderIntent(action="short", symbol="B", quantity=5.4, group_id="spread"),
        ],
        instruments,
        atomic_groups=True,
    )

    assert [event.fill_quantity for event in result.events] == [2.0, 4.0]


def test_portfolio_weight_sizing_is_normalized_before_execution() -> None:
    instruments = {"FUT": _instrument("FUT", quantity_step=1.0, min_quantity=1.0)}

    result = execute_portfolio_weights(
        PortfolioWeights({"FUT": 1.0}),
        {},
        1_000.0,
        TS,
        get_price=lambda _symbol, _intent: 300.0,
        get_cost_model=lambda _symbol: CostModel.zero(),
        primary_symbol="FUT",
        get_executable_quantity=_normalizer(instruments),
    )

    assert result.events[0].fill_quantity == 3.0


def _long_future_with_stop() -> PositionState:
    return PositionState(
        symbol="FUT",
        side="long",
        entry_price=100.0,
        quantity=5.0,
        entry_at=TS,
        periods_held=1,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=500.0,
        stop_price=90.0,
    )


def test_volume_limited_protective_exit_keeps_whole_contracts() -> None:
    instrument = _instrument("FUT", quantity_step=1.0, min_quantity=1.0)
    positions = {"FUT": _long_future_with_stop()}

    result = check_stop_targets(
        positions,
        {"FUT": {"open": 85.0, "high": 90.0, "low": 80.0, "volume": 2.9}},
        TS,
        get_cost_model=lambda _symbol: CostModel.zero(),
        max_bar_volume_participation_rate=1.0,
        get_executable_quantity=lambda _symbol, quantity: instrument.normalize_quantity(quantity),
    )

    assert result.events[0].fill_quantity == 2.0
    assert positions["FUT"].quantity == 3.0


def test_volume_limited_forced_close_keeps_whole_contracts() -> None:
    instrument = _instrument("FUT", quantity_step=1.0, min_quantity=1.0)
    positions = {"FUT": _long_future_with_stop()}

    result = liquidate_all(
        positions,
        {"FUT": {"close": 100.0, "volume": 2.9}},
        TS,
        get_cost_model=lambda _symbol: CostModel.zero(),
        reason=REASON_FORCE_CLOSE,
        max_bar_volume_participation_rate=1.0,
        get_executable_quantity=lambda _symbol, quantity: instrument.normalize_quantity(quantity),
    )

    assert result.events[0].fill_quantity == 2.0
    assert positions["FUT"].quantity == 3.0
