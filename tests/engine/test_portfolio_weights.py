"""Portfolio target-rebalance execution and snapshot tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    ExecutionResult,
    execute_pending_decision_and_stops,
    execute_portfolio_weights,
)
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import (
    Context,
    OrderIntent,
    PortfolioWeights,
    PositionState,
    Strategy,
    StrategyDecision,
)

TS = datetime(2026, 7, 27, tzinfo=UTC)


def _process(
    targets: PortfolioWeights,
    positions: dict[str, PositionState],
    cash: float,
    *,
    prices: dict[str, float],
    cost_model: CostModel | None = None,
    max_bar_volume_participation_rate: float | None = None,
    volumes: dict[str, float] | None = None,
) -> ExecutionResult:
    model = cost_model or CostModel.zero()
    return execute_portfolio_weights(
        targets,
        positions,
        cash,
        TS,
        get_price=lambda symbol, _action: prices.get(symbol),
        get_reference_price=prices.get,
        get_cost_model=lambda _symbol: model,
        primary_symbol="A",
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        get_volume=(lambda symbol: volumes.get(symbol)) if volumes is not None else None,
    )


def _multi_asset_frame(
    opens: dict[str, list[float]],
    closes: dict[str, list[float]] | None = None,
    *,
    freq: str = "h",
) -> pd.DataFrame:
    timestamps = pd.date_range(
        "2026-01-01",
        periods=len(next(iter(opens.values()))),
        freq=freq,
        tz="UTC",
    )
    rows: list[dict[str, float | str | pd.Timestamp]] = []
    closes = closes or opens
    for symbol in sorted(opens):
        for index, timestamp in enumerate(timestamps):
            open_price = opens[symbol][index]
            close_price = closes[symbol][index]
            rows.append(
                {
                    "symbol": symbol,
                    "datetime": timestamp,
                    "open": open_price,
                    "high": max(open_price, close_price),
                    "low": min(open_price, close_price),
                    "close": close_price,
                    "volume": 10_000.0,
                }
            )
    return pd.DataFrame(rows).set_index(["symbol", "datetime"])


class OneRebalance(Strategy):
    """Submit one equal-weight target portfolio on the first bar."""

    def __init__(self) -> None:
        self.seen_equity: list[float] = []

    def on_bar(self, ctx: Context) -> StrategyDecision:
        self.seen_equity.append(ctx.equity)
        if ctx.period_index == 0:
            return PortfolioWeights(weights={"A": 0.5, "B": 0.5})
        return []


class TestPortfolioWeightsValidation:
    def test_rejects_non_finite_weight(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            PortfolioWeights(weights={"A": float("nan")})

    def test_weights_are_immutable_after_validation(self) -> None:
        targets = PortfolioWeights(weights={"A": 1.0})

        with pytest.raises(TypeError):
            targets.weights["A"] = float("nan")


class TestOrderIntentValidation:
    @pytest.mark.parametrize("quantity", [0.0, -1.0, float("nan"), float("inf"), True])
    def test_quantity_must_be_positive_and_finite(self, quantity) -> None:
        with pytest.raises(ValueError, match="quantity"):
            OrderIntent(action="long", symbol="A", quantity=quantity)

    def test_invalid_action_fails_at_construction(self) -> None:
        with pytest.raises(ValueError, match="action"):
            OrderIntent(action="buy", symbol="A")

    def test_close_cannot_set_protective_prices(self) -> None:
        with pytest.raises(ValueError, match="close intents"):
            OrderIntent(action="close", symbol="A", stop_price=90.0)


class TestRebalanceExecution:
    def test_opens_equal_weight_long_only_portfolio(self) -> None:
        positions: dict[str, PositionState] = {}
        result = _process(
            PortfolioWeights(weights={"B": 0.5, "A": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
        )

        assert [event.symbol for event in result.events] == ["A", "B"]
        assert np.isclose(positions["A"].quantity, 5.0)
        assert np.isclose(positions["B"].quantity, 10.0)
        assert np.isclose(result.cash_delta, -1_000.0)

    def test_reduces_before_adding(self) -> None:
        positions: dict[str, PositionState] = {}
        first = _process(
            PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
        )
        cash = 1_000.0 + first.cash_delta

        second = _process(
            PortfolioWeights(weights={"A": 0.25, "B": 0.75}),
            positions,
            cash,
            prices={"A": 100.0, "B": 50.0},
        )

        assert [(event.event_type, event.symbol) for event in second.events] == [
            ("reduce", "A"),
            ("add", "B"),
        ]
        assert np.isclose(positions["A"].quantity, 2.5)
        assert np.isclose(positions["B"].quantity, 15.0)

    def test_omitted_asset_is_closed(self) -> None:
        positions: dict[str, PositionState] = {}
        first = _process(
            PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
        )
        cash = 1_000.0 + first.cash_delta

        second = _process(
            PortfolioWeights(weights={"B": 1.0}),
            positions,
            cash,
            prices={"A": 100.0, "B": 50.0},
        )

        assert "A" not in positions
        assert np.isclose(positions["B"].quantity, 20.0)
        assert [event.event_type for event in second.events] == ["close", "add"]

    def test_cost_shortfall_scales_all_additions_proportionally(self) -> None:
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.01,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        positions: dict[str, PositionState] = {}

        result = _process(
            PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
            cost_model=cost_model,
        )

        assert len(result.events) == 2
        assert np.isclose(positions["A"].quantity * 100.0, positions["B"].quantity * 50.0)
        assert np.isclose(1_000.0 + result.cash_delta, 0.0, atol=1e-7)
        assert positions["A"].quantity < 5.0
        assert positions["B"].quantity < 10.0

    def test_insufficient_cash_reports_a_runtime_event_and_skips_additions(self) -> None:
        """A minimum-commission floor that dwarfs available cash makes any
        nonzero scale unaffordable — additions are dropped entirely, and the
        drop must be reported as a RuntimeEvent, not just a log line."""
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.01,
            min_commission=1e12,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        positions: dict[str, PositionState] = {}

        result = _process(
            PortfolioWeights(weights={"A": 1.0}),
            positions,
            1_000_000.0,
            prices={"A": 100.0},
            cost_model=cost_model,
        )

        assert result.events == []
        assert positions == {}
        assert len(result.runtime_events) == 1
        event = result.runtime_events[0]
        assert event.event_type == "decision_skipped"
        assert event.detail["reason"] == "insufficient_cash"
        assert event.detail["symbols"] == ["A"]

    def test_zero_cash_additions_report_insufficient_cash(self) -> None:
        positions: dict[str, PositionState] = {}
        initial = _process(
            PortfolioWeights(weights={"A": 1.0}),
            positions,
            1_000.0,
            prices={"A": 100.0},
        )
        cash = 1_000.0 + initial.cash_delta

        result = _process(
            PortfolioWeights(weights={"A": 1.0, "B": 0.1}),
            positions,
            cash,
            prices={"A": 100.0, "B": 50.0},
        )

        assert np.isclose(cash, 0.0)
        assert result.events == []
        assert "B" not in positions
        assert len(result.runtime_events) == 1
        event = result.runtime_events[0]
        assert event.event_type == "decision_skipped"
        assert event.detail == {
            "reason": "insufficient_cash",
            "symbols": ["B"],
            "available_cash": pytest.approx(0.0),
        }

    def test_runtime_events_preserve_reduction_then_addition_order(self) -> None:
        positions: dict[str, PositionState] = {}
        initial = _process(
            PortfolioWeights(weights={"A": 0.5}),
            positions,
            2_000.0,
            prices={"A": 100.0},
        )
        cash = 2_000.0 + initial.cash_delta

        result = _process(
            PortfolioWeights(weights={"A": 0.25, "B": 0.25}),
            positions,
            cash,
            prices={"A": 100.0, "B": 50.0},
            max_bar_volume_participation_rate=0.1,
            volumes={"A": 0.0, "B": 0.0},
        )

        assert result.events == []
        assert [event.symbol for event in result.runtime_events] == ["A", "B"]
        assert [event.detail["reason"] for event in result.runtime_events] == [
            "volume_capped",
            "volume_capped",
        ]

    def test_reduction_runtime_event_precedes_cash_event(self) -> None:
        positions: dict[str, PositionState] = {}
        initial = _process(
            PortfolioWeights(weights={"A": 1.0}),
            positions,
            1_000.0,
            prices={"A": 100.0},
        )
        cash = 1_000.0 + initial.cash_delta

        result = _process(
            PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
            positions,
            cash,
            prices={"A": 100.0, "B": 50.0},
            max_bar_volume_participation_rate=0.1,
            volumes={"A": 0.0, "B": 1_000.0},
        )

        assert result.events == []
        assert [event.symbol for event in result.runtime_events] == ["A", None]
        assert [event.detail["reason"] for event in result.runtime_events] == [
            "volume_capped",
            "insufficient_cash",
        ]
        assert result.runtime_events[1].detail["symbols"] == ["B"]

    def test_weight_remainder_stays_in_cash(self) -> None:
        positions: dict[str, PositionState] = {}
        result = _process(
            PortfolioWeights(weights={"A": 0.95}),
            positions,
            1_000.0,
            prices={"A": 100.0},
        )

        assert np.isclose(positions["A"].quantity, 9.5)
        assert np.isclose(1_000.0 + result.cash_delta, 50.0)

    def test_missing_price_rejects_batch_without_mutation(self) -> None:
        positions: dict[str, PositionState] = {}
        with pytest.raises(ValueError, match="execution price for B"):
            _process(
                PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
                positions,
                1_000.0,
                prices={"A": 100.0},
            )

        assert positions == {}

    def test_rebalance_checks_the_actual_order_side(self) -> None:
        positions: dict[str, PositionState] = {}
        first = _process(
            PortfolioWeights(weights={"A": 1.0}),
            positions,
            1_000.0,
            prices={"A": 100.0},
        )
        cash = 1_000.0 + first.cash_delta

        result = execute_portfolio_weights(
            PortfolioWeights(weights={"A": 0.5}),
            positions,
            cash,
            TS,
            get_price=lambda _symbol, action: 100.0 if action.action == "close" else None,
            get_reference_price=lambda _symbol: 100.0,
            get_cost_model=lambda _symbol: CostModel.zero(),
            primary_symbol="A",
        )

        assert [(event.event_type, event.fill_quantity) for event in result.events] == [
            ("reduce", pytest.approx(5.0))
        ]

    def test_fail_policy_stages_before_rejecting_a_partial_fill(self) -> None:
        positions: dict[str, PositionState] = {}
        used_adv_quantity = {"A": 2.0}
        bars = {
            "A": {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 10.0,
            }
        }

        with pytest.raises(ValueError, match="fail policy rejected incomplete execution"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                1_000.0,
                PortfolioWeights(weights={"A": 0.3}),
                bars,
                get_cost_model=lambda _symbol: CostModel.zero(),
                default_fill="open",
                primary_symbol="A",
                max_bar_volume_participation_rate=0.1,
                used_adv_quantity_by_symbol=used_adv_quantity,
                rebalance_residual_policy="fail",
            )

        assert positions == {}
        assert used_adv_quantity == {"A": 2.0}

    def test_fail_policy_atomically_rejects_initial_missing_symbol(self) -> None:
        positions: dict[str, PositionState] = {}
        bars = {
            "A": {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
            }
        }

        with pytest.raises(ValueError, match=r"residual remains for \['B'\]"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                1_000.0,
                PortfolioWeights(weights={"A": 0.5, "B": 0.5}),
                bars,
                get_cost_model=lambda _symbol: CostModel.zero(),
                default_fill="open",
                primary_symbol="A",
                max_bar_volume_participation_rate=None,
                exposure_prices={"A": 100.0, "B": 100.0},
                rebalance_residual_policy="fail",
            )

        assert positions == {}

    def test_retained_policy_stages_reduction_before_max_order_failure(self) -> None:
        positions: dict[str, PositionState] = {}
        seed = _process(
            PortfolioWeights(weights={"A": 0.4}),
            positions,
            1_000.0,
            prices={"A": 100.0},
        )
        cash = 1_000.0 + seed.cash_delta
        used_adv_quantity = {"A": 2.0}
        bars = {
            symbol: {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
            }
            for symbol in ("A", "B")
        }

        with pytest.raises(ValueError, match="max_order_notional"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                cash,
                PortfolioWeights(weights={"B": 1.0}),
                bars,
                get_cost_model=lambda _symbol: CostModel.zero(),
                default_fill="open",
                primary_symbol="A",
                max_order_notional=500.0,
                max_bar_volume_participation_rate=None,
                used_adv_quantity_by_symbol=used_adv_quantity,
                exposure_prices={"A": 100.0, "B": 100.0},
                rebalance_residual_policy="defer_symbols",
            )

        assert positions["A"].quantity == pytest.approx(4.0)
        assert "B" not in positions
        assert used_adv_quantity == {"A": 2.0}

    def test_fail_policy_rejects_position_cap_without_mutation(self) -> None:
        positions: dict[str, PositionState] = {}
        bars = {
            "A": {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
            }
        }

        with pytest.raises(ValueError, match="exceeds max_position_notional"):
            execute_pending_decision_and_stops(
                TS,
                positions,
                1_000.0,
                PortfolioWeights(weights={"A": 0.5}),
                bars,
                get_cost_model=lambda _symbol: CostModel.zero(),
                default_fill="open",
                primary_symbol="A",
                max_position_notional=200.0,
                max_bar_volume_participation_rate=None,
                exposure_prices={"A": 100.0},
                rebalance_residual_policy="fail",
            )

        assert positions == {}

    def test_deferred_policy_audits_and_cancels_position_cap_excess(self) -> None:
        positions: dict[str, PositionState] = {}
        bars = {
            "A": {
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 100.0,
            }
        }

        _, result = execute_pending_decision_and_stops(
            TS,
            positions,
            1_000.0,
            PortfolioWeights(weights={"A": 0.5}),
            bars,
            get_cost_model=lambda _symbol: CostModel.zero(),
            default_fill="open",
            primary_symbol="A",
            max_position_notional=200.0,
            max_bar_volume_participation_rate=None,
            exposure_prices={"A": 100.0},
            rebalance_residual_policy="defer_symbols",
        )

        assert positions["A"].quantity == pytest.approx(2.0)
        assert result.pending_rebalance is None
        constrained = next(
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_constrained_by_position_limit"
        )
        assert constrained.detail["requested_quantity"] == pytest.approx(5.0)
        assert constrained.detail["filled_quantity"] == pytest.approx(2.0)
        assert constrained.detail["cancelled_quantity"] == pytest.approx(3.0)
        assert constrained.detail["remaining_quantity"] == 0.0


class TestBacktestRebalance:
    def test_targets_fill_next_bar_at_execution_prices(self) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 120.0, 120.0, 120.0, 120.0],
                "B": [200.0, 240.0, 240.0, 240.0, 240.0],
            }
        )
        strategy = OneRebalance()
        backtest = Backtest(
            frame,
            strategy,
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
        )

        result = backtest.run()
        open_events = [event for event in result.position_events if event.event_type == "open"]

        assert strategy.seen_equity[0] == 1_000.0
        assert [event.price for event in open_events] == [120.0, 240.0]
        assert np.isclose(open_events[0].fill_quantity, 1_000.0 * 0.5 / 120.0)
        assert np.isclose(open_events[1].fill_quantity, 1_000.0 * 0.5 / 240.0)
        assert all(
            event.ts == frame.index.get_level_values("datetime").unique()[1]
            for event in open_events
        )

    def test_initial_missing_symbol_does_not_block_defer_symbols(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5, "B": [100.0] * 5})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=("B", timestamps[1]))

        result = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        opens = [event for event in result.position_events if event.event_type == "open"]
        assert [(event.symbol, event.ts, event.fill_quantity) for event in opens] == [
            ("A", timestamps[1], pytest.approx(5.0)),
            ("B", timestamps[2], pytest.approx(5.0)),
        ]
        unresolved = next(
            event
            for event in result.runtime_events
            if event.symbol == "B" and event.detail.get("sizing_state") == "awaiting_fresh_price"
        )
        assert unresolved.detail["notional_scope"] == "target_allocation"
        assert unresolved.detail["requested_notional"] == pytest.approx(500.0)
        assert unresolved.detail["filled_notional"] == 0.0
        assert unresolved.detail["remaining_notional"] == pytest.approx(500.0)
        assert "requested_quantity" not in unresolved.detail

    def test_initial_missing_symbol_sizes_from_first_fresh_price(self) -> None:
        frame = _multi_asset_frame(
            opens={"A": [100.0] * 5, "B": [100.0, 100.0, 200.0, 200.0, 200.0]}
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=("B", timestamps[1]))

        result = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        b_open = next(
            event
            for event in result.position_events
            if event.symbol == "B" and event.event_type == "open"
        )
        assert (b_open.ts, b_open.price, b_open.fill_quantity) == (
            timestamps[2],
            200.0,
            pytest.approx(2.5),
        )

    def test_cash_scaled_tail_is_final_when_no_reduction_is_pending(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        cost_model = replace(CostModel.zero(), commission_rate=0.01)

        class FullyInvested(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 1.0})
                return []

        result = Backtest(
            frame,
            FullyInvested(),
            initial_balance=1_000.0,
            cost_model=cost_model,
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entries = [event for event in result.position_events if event.event_type == "open"]
        assert len(entries) == 1
        assert entries[0].fill_quantity == pytest.approx(1_000.0 / 101.0)
        assert not [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual"
        ]
        constrained = next(
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_constrained_by_cash"
        )
        assert constrained.detail["requested_quantity"] == pytest.approx(10.0)
        assert constrained.detail["filled_quantity"] == pytest.approx(1_000.0 / 101.0)
        assert constrained.detail["cancelled_quantity"] == pytest.approx(10.0 - 1_000.0 / 101.0)
        assert constrained.detail["remaining_quantity"] == 0.0

    def test_unresolved_new_symbol_does_not_keep_cash_tail_alive(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5, "B": [100.0] * 5})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=("B", timestamps[1]))
        cost_model = replace(CostModel.zero(), commission_rate=0.01)

        class SparseLeveredTarget(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 1.0, "B": 0.5})
                return []

        result = Backtest(
            frame,
            SparseLeveredTarget(),
            initial_balance=1_000.0,
            cost_model=cost_model,
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        a_open = next(event for event in result.position_events if event.symbol == "A")
        assert a_open.fill_quantity == pytest.approx(1_000.0 / 101.0)
        assert not [
            event
            for event in result.runtime_events
            if event.symbol == "A" and event.detail.get("reason") == "rebalance_residual"
        ]
        b_cancellation = next(
            event
            for event in result.runtime_events
            if event.symbol == "B" and event.detail.get("reason") == "rebalance_constrained_by_cash"
        )
        assert b_cancellation.detail["requested_quantity"] == pytest.approx(5.0)
        assert b_cancellation.detail["filled_quantity"] == 0.0
        assert b_cancellation.detail["cancelled_quantity"] == pytest.approx(5.0)
        assert b_cancellation.detail["remaining_quantity"] == 0.0

    def test_residual_slice_is_covered_by_the_ambiguous_ordering_guard(self) -> None:
        """A carried residual fills at this bar's price like any other
        decision, so it faces the same unresolvable ordering against a
        protection triggered on the same bar. The guard keys on the pending
        decision, which a residual slice leaves empty, so it has to be told
        about the residual explicitly or those bars -- the ones most likely to
        collide, since a residual persists across bars -- would slip through.
        """
        frame = _multi_asset_frame(opens={"A": [100.0] * 6}, closes={"A": [100.0] * 6})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 1.0
        # The stop triggers on the bar where the residual would slice again.
        frame.loc[("A", timestamps[3]), "low"] = 80.0

        class SlowTargetWithStop(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="A", quantity=1.0, stop_price=95.0)]
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": 0.5})
                return []

        with pytest.raises(ValueError, match="ambiguous same-bar ordering"):
            Backtest(
                frame,
                SlowTargetWithStop(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    default_fill_price="close",
                    max_bar_volume_participation_rate=1.0,
                    max_rebalance_delay_bars=4,
                    rebalance_residual_policy="defer_symbols",
                ),
            ).run()

    def test_triggered_stop_cancels_addition_residual_and_owns_later_capacity(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 6})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 1.0
        frame.loc[("A", timestamps[2]), "low"] = 90.0

        class AddIntoStop(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return [
                        OrderIntent(
                            action="long",
                            symbol="A",
                            quantity=1.0,
                            stop_price=95.0,
                        )
                    ]
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": 0.3})
                return []

        result = Backtest(
            frame,
            AddIntoStop(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=4,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        additions = [event for event in result.position_events if event.event_type == "add"]
        stop_exits = [event for event in result.position_events if event.reason == "stop_loss"]
        assert [(event.ts, event.fill_quantity) for event in additions] == [
            (timestamps[2], pytest.approx(1.0))
        ]
        assert [(event.ts, event.fill_quantity) for event in stop_exits] == [
            (timestamps[3], pytest.approx(1.0)),
            (timestamps[4], pytest.approx(1.0)),
        ]
        keys = [(event.ts, event.event_type, event.symbol) for event in result.runtime_events]
        assert len(keys) == len(set(keys))
        stop_audit = next(
            event
            for event in result.runtime_events
            if event.ts == timestamps[2] and event.symbol == "A"
        )
        assert stop_audit.detail["reason"] == "rebalance_cancelled_by_protective_exit"
        related_reasons = {
            detail["reason"] for detail in stop_audit.detail.get("related_events", [])
        }
        assert related_reasons == {
            "rebalance_residual",
            "protective_exit_deferred",
        }

    @pytest.mark.parametrize("closed_market_representation", ["missing", "untradable"])
    def test_rebalance_defers_whole_book_until_every_leg_is_tradable(
        self,
        closed_market_representation: str,
    ) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 110.0, 120.0, 120.0, 120.0],
                "B": [200.0, 200.0, 240.0, 240.0, 240.0],
            }
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        if closed_market_representation == "missing":
            frame = frame.drop(index=("B", timestamps[1]))
        else:
            frame["can_buy"] = True
            frame["can_sell"] = True
            frame.loc[("B", timestamps[1]), ["can_buy", "can_sell"]] = False

        result = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
            ),
        ).run()

        open_events = [event for event in result.position_events if event.event_type == "open"]
        assert [(event.symbol, event.ts, event.price) for event in open_events] == [
            ("A", timestamps[2], 120.0),
            ("B", timestamps[2], 240.0),
        ]

    def test_deferred_target_is_not_reported_as_active(self) -> None:
        """A deferred target has not been attempted, so the allocation
        snapshot on the deferral bar must not report it as the active target
        or measure drift against a book that still reflects the last executed
        one -- that would print a phantom target on every deferral bar."""
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 110.0, 120.0, 120.0, 120.0],
                "B": [200.0, 200.0, 240.0, 240.0, 240.0],
            }
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=("B", timestamps[1]))

        backtest = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
            ),
            record_position_snapshots=True,
        )
        backtest.run()
        output = backtest.build_output()

        by_ts = {}
        for snapshot in output.allocation_snapshots:
            by_ts.setdefault(snapshot.ts, {})[snapshot.symbol] = snapshot
        deferred = by_ts[timestamps[1]]
        executed = by_ts[timestamps[2]]

        assert all(snapshot.target_weight is None for snapshot in deferred.values())
        assert all(snapshot.weight_drift is None for snapshot in deferred.values())
        assert all(np.isclose(snapshot.target_weight, 0.5) for snapshot in executed.values())

    def test_rebalance_fails_when_delay_bound_is_exceeded(self) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 110.0, 120.0, 120.0, 120.0],
                "B": [200.0, 200.0, 200.0, 240.0, 240.0],
            }
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=[("B", timestamps[1]), ("B", timestamps[2])])

        with pytest.raises(
            ValueError,
            match=r"max_rebalance_delay_bars=1.*\['B'\]",
        ):
            Backtest(
                frame,
                OneRebalance(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    max_rebalance_delay_bars=1,
                ),
            ).run()

    def test_deferred_rebalance_fails_if_the_backtest_ends(self) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 100.0, 100.0, 100.0, 110.0],
                "B": [200.0, 200.0, 200.0, 200.0, 200.0],
            }
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame = frame.drop(index=("B", timestamps[-1]))

        class LateRebalance(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 3:
                    return PortfolioWeights(weights={"A": 0.5, "B": 0.5})
                return []

        with pytest.raises(ValueError, match="ended before deferred PortfolioWeights"):
            Backtest(
                frame,
                LateRebalance(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    max_rebalance_delay_bars=2,
                ),
            ).run()

    def test_new_target_supersedes_a_deferred_rebalance(self) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 110.0, 120.0, 120.0, 120.0],
                "B": [200.0, 200.0, 240.0, 240.0, 240.0],
            }
        )
        frame["can_buy"] = True
        frame["can_sell"] = True
        timestamps = frame.index.get_level_values("datetime").unique()
        frame.loc[("B", timestamps[1]), ["can_buy", "can_sell"]] = False

        class RevisedRebalance(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.5, "B": 0.5})
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": 0.25, "B": 0.75})
                return []

        result = Backtest(
            frame,
            RevisedRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
            ),
        ).run()

        opens = [event for event in result.position_events if event.event_type == "open"]
        assert [(event.symbol, event.ts, event.notional) for event in opens] == [
            ("A", timestamps[2], pytest.approx(250.0)),
            ("B", timestamps[2], pytest.approx(750.0)),
        ]
        superseded = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_superseded"
        ]
        assert [(event.ts, event.symbol) for event in superseded] == [(timestamps[1], None)]

    def test_defer_all_waits_for_a_halted_symbol_before_slicing(self) -> None:
        frame = _multi_asset_frame(
            opens={"A": [100.0] * 5, "B": [200.0] * 5},
        )
        frame["can_buy"] = True
        frame["can_sell"] = True
        timestamps = frame.index.get_level_values("datetime").unique()
        frame.loc[("B", timestamps[1]), "can_buy"] = False

        result = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_all",
            ),
        ).run()

        opens = [event for event in result.position_events if event.event_type == "open"]
        assert [(event.symbol, event.ts) for event in opens] == [
            ("A", timestamps[2]),
            ("B", timestamps[2]),
        ]
        deferred = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual" and event.ts == timestamps[1]
        ]
        assert [(event.symbol, event.detail["filled_quantity"]) for event in deferred] == [
            ("A", 0.0),
            ("B", 0.0),
        ]
        assert all(event.detail["blocked_symbols"] == ["B"] for event in deferred)

    def test_order_intents_while_a_residual_is_pending_raise(self) -> None:
        """A per-symbol order cannot be sequenced against a whole-book target
        that is still filling, so the strategy must return nothing or a
        newer PortfolioWeights until the residual clears."""
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 1.0

        class IntentDuringResidual(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.3})
                if ctx.period_index == 2:
                    return [OrderIntent(action="close", symbol="A")]
                return []

        with pytest.raises(ValueError, match="rebalance is deferred"):
            Backtest(
                frame,
                IntentDuringResidual(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=1.0,
                    max_rebalance_delay_bars=4,
                    rebalance_residual_policy="defer_symbols",
                ),
            ).run()

    def test_drawdown_halt_records_the_residual_it_cancels(self) -> None:
        """Every other path that drops a residual leaves an audit event
        (supersession, protective exit). A drawdown halt must too, or the
        event log shows a target that simply stops filling."""
        frame = _multi_asset_frame(opens={"A": [100.0, 100.0, 100.0, 50.0, 50.0, 50.0, 50.0]})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 1.0

        class SlowTarget(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.5})
                return []

        result = Backtest(
            frame,
            SlowTarget(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=8,
                rebalance_residual_policy="defer_symbols",
            ),
            risk=RiskPolicy(max_drawdown_rate=0.05),
        ).run()

        cancelled = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_cancelled_by_halt"
        ]
        assert [(event.ts, event.symbol) for event in cancelled] == [(timestamps[3], "A")]
        assert cancelled[0].detail["remaining_quantity"] > 0
        assert not any(
            event.ts > timestamps[3] and event.detail.get("reason") == "rebalance_residual"
            for event in result.runtime_events
        )

    def test_defer_symbols_allows_independent_progress(self) -> None:
        frame = _multi_asset_frame(
            opens={"A": [100.0] * 5, "B": [200.0] * 5},
        )
        frame["can_buy"] = True
        frame["can_sell"] = True
        timestamps = frame.index.get_level_values("datetime").unique()
        frame.loc[("B", timestamps[1]), "can_buy"] = False

        result = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        opens = [event for event in result.position_events if event.event_type == "open"]
        assert [(event.symbol, event.ts) for event in opens] == [
            ("A", timestamps[1]),
            ("B", timestamps[2]),
        ]
        residual = next(
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual"
        )
        assert residual.symbol == "B"
        assert residual.detail["quantity_scope"] == "attempt"
        assert residual.detail["requested_quantity"] == pytest.approx(2.5)
        assert residual.detail["filled_quantity"] == 0.0
        assert residual.detail["remaining_quantity"] == pytest.approx(2.5)

    def test_missing_bar_defers_only_that_symbols_existing_residual(self) -> None:
        frame = _multi_asset_frame(
            opens={"A": [100.0] * 6, "B": [100.0] * 6},
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 1.0
        final_bars = frame.index.get_level_values("datetime").isin(timestamps[4:])
        frame.loc[final_bars, "volume"] = 100.0
        frame = frame.drop(index=("B", timestamps[2]))

        class TargetBoth(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.2, "B": 0.2})
                return []

        result = Backtest(
            frame,
            TargetBoth(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entries = [event for event in result.position_events if event.event_type in ("open", "add")]
        assert [(event.symbol, event.ts, event.fill_quantity) for event in entries] == [
            ("A", timestamps[1], pytest.approx(1.0)),
            ("B", timestamps[1], pytest.approx(1.0)),
            ("A", timestamps[2], pytest.approx(1.0)),
            ("B", timestamps[3], pytest.approx(1.0)),
        ]

    def test_volume_limited_target_is_sliced_across_bars(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 6})
        frame["volume"] = 10.0
        timestamps = frame.index.get_level_values("datetime").unique()

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.3})
                return []

        result = Backtest(
            frame,
            TargetA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entries = [
            event
            for event in result.position_events
            if event.reason == "" and event.event_type in ("open", "add")
        ]
        assert [(event.ts, event.fill_quantity) for event in entries] == [
            (timestamps[1], pytest.approx(1.0)),
            (timestamps[2], pytest.approx(1.0)),
            (timestamps[3], pytest.approx(1.0)),
        ]
        residuals = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual"
        ]
        assert [
            (
                event.detail["requested_quantity"],
                event.detail["filled_quantity"],
                event.detail["remaining_quantity"],
            )
            for event in residuals
        ] == [
            (pytest.approx(3.0), pytest.approx(1.0), pytest.approx(2.0)),
            (pytest.approx(2.0), pytest.approx(1.0), pytest.approx(1.0)),
        ]

    def test_short_target_residual_is_sliced_across_bars(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 10.0
        final_timestamp = frame.index.get_level_values("datetime").unique()[-1]
        frame.loc[("A", final_timestamp), "volume"] = 100.0

        class ShortA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": -0.2})
                return []

        result = Backtest(
            frame,
            ShortA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entries = [event for event in result.position_events if event.event_type in ("open", "add")]
        assert [(event.side, event.fill_quantity) for event in entries] == [
            ("short", pytest.approx(1.0)),
            ("short", pytest.approx(1.0)),
        ]

    def test_zero_volume_defers_without_manufacturing_a_fill(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 100.0
        frame.loc[("A", timestamps[0]), "volume"] = 0.0
        frame.loc[("A", timestamps[1]), "volume"] = 10.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.1})
                return []

        result = Backtest(
            frame,
            TargetA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entry = next(event for event in result.position_events if event.event_type == "open")
        assert (entry.ts, entry.fill_quantity) == (timestamps[2], pytest.approx(1.0))
        residual = next(
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual"
        )
        assert residual.ts == timestamps[1]
        assert residual.detail["requested_quantity"] == pytest.approx(1.0)
        assert residual.detail["filled_quantity"] == 0.0
        assert residual.detail["remaining_quantity"] == pytest.approx(1.0)

    def test_discard_policy_preserves_one_bar_volume_behavior(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 10.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.3})
                return []

        result = Backtest(
            frame,
            TargetA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="discard",
            ),
        ).run()

        entries = [event for event in result.position_events if event.event_type in ("open", "add")]
        assert [event.fill_quantity for event in entries] == [pytest.approx(1.0)]
        assert not [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_residual"
        ]

    def test_fail_policy_rejects_partial_fill_without_mutation(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 10.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.3})
                return []

        backtest = Backtest(
            frame,
            TargetA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                rebalance_residual_policy="fail",
            ),
        )

        with pytest.raises(
            ValueError,
            match=r"fail policy rejected incomplete execution.*\['A'\]",
        ):
            backtest.run()

    def test_adv_limited_target_is_sliced_across_sessions(self) -> None:
        frame = _multi_asset_frame(opens={"A": [10.0] * 6}, freq="D")
        frame["volume"] = 100.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.25})
                return []

        result = Backtest(
            frame,
            TargetA(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                adv_lookback_sessions=1,
                max_adv_participation_rate=0.1,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        entries = [event for event in result.position_events if event.event_type in ("open", "add")]
        assert [event.fill_quantity for event in entries] == [
            pytest.approx(10.0),
            pytest.approx(10.0),
            pytest.approx(5.0),
        ]

    def test_rebalance_residual_expires_at_the_configured_bound(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 10.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.3})
                return []

        with pytest.raises(
            ValueError,
            match=r"max_rebalance_delay_bars=1.*residual remains for \['A'\]",
        ):
            Backtest(
                frame,
                TargetA(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=0.1,
                    max_rebalance_delay_bars=1,
                    rebalance_residual_policy="defer_symbols",
                ),
            ).run()

    def test_terminal_residual_fails_instead_of_being_discarded(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 5})
        frame["volume"] = 10.0

        class TargetA(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.6})
                return []

        with pytest.raises(
            ValueError,
            match=r"ended before deferred PortfolioWeights.*\['A'\]",
        ):
            Backtest(
                frame,
                TargetA(),
                initial_balance=1_000.0,
                cost_model=CostModel.zero(),
                data_source="test",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=0.1,
                    max_rebalance_delay_bars=10,
                    rebalance_residual_policy="defer_symbols",
                ),
            ).run()

    def test_new_target_supersedes_partially_filled_quantities(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 6})
        frame["volume"] = 10.0
        timestamps = frame.index.get_level_values("datetime").unique()

        class RevisedTarget(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.5}, reason="old")
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": 0.2}, reason="new")
                return []

        result = Backtest(
            frame,
            RevisedTarget(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=0.1,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        target_entries = [
            event for event in result.position_events if event.event_type in ("open", "add")
        ]
        assert [(event.ts, event.reason, event.fill_quantity) for event in target_entries] == [
            (timestamps[1], "old", pytest.approx(1.0)),
            (timestamps[2], "new", pytest.approx(1.0)),
        ]
        superseded = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "rebalance_superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0].symbol == "A"
        assert superseded[0].detail["quantity_scope"] == "target"
        assert superseded[0].detail["requested_quantity"] == pytest.approx(5.0)
        assert superseded[0].detail["filled_quantity"] == pytest.approx(1.0)
        assert superseded[0].detail["remaining_quantity"] == pytest.approx(4.0)

    def test_reversal_supersession_aggregates_phases_to_one_durable_event(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 6})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 100.0
        frame.loc[("A", timestamps[1]), "volume"] = 1.0

        class SupersedeReversal(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.2}, reason="seed")
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": -0.2}, reason="reverse")
                if ctx.period_index == 2:
                    return PortfolioWeights(weights={"A": 0.0}, reason="flatten")
                return []

        result = Backtest(
            frame,
            SupersedeReversal(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        at_supersession = [
            event
            for event in result.runtime_events
            if event.ts == timestamps[2]
            and event.event_type == "decision_skipped"
            and event.symbol == "A"
        ]
        assert len(at_supersession) == 1
        event = at_supersession[0]
        assert event.detail["reason"] == "rebalance_superseded"
        assert [phase["phase"] for phase in event.detail["phases"]] == [
            "reduction",
            "addition",
        ]
        assert event.detail["requested_quantity"] == pytest.approx(4.0)
        assert event.detail["filled_quantity"] == pytest.approx(1.0)
        assert event.detail["remaining_quantity"] == pytest.approx(3.0)
        assert event.detail["related_events"][0]["reason"] == "rebalance_residual"

    def test_additions_use_only_cash_realized_by_reduction_slices(self) -> None:
        frame = _multi_asset_frame(
            opens={"A": [100.0] * 6, "B": [100.0] * 6},
        )
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 100.0
        frame.loc[("A", timestamps[1]), "volume"] = 1.0
        frame.loc[("A", timestamps[2]), "volume"] = 9.0

        class Rotate(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 1.0}, reason="seed")
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"B": 1.0}, reason="rotate")
                return []

        result = Backtest(
            frame,
            Rotate(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=2,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        first_slice = [
            event
            for event in result.position_events
            if event.ts == timestamps[2] and event.reason == "rotate"
        ]
        assert [(event.symbol, event.fill_quantity) for event in first_slice] == [
            ("A", pytest.approx(1.0)),
            ("B", pytest.approx(1.0)),
        ]
        assert sum(event.cash_flow or 0.0 for event in first_slice) == pytest.approx(0.0)
        b_additions = [
            event
            for event in result.position_events
            if event.symbol == "B" and event.event_type in ("open", "add")
        ]
        assert sum(event.fill_quantity for event in b_additions) == pytest.approx(10.0)

    def test_reversal_finishes_reduction_before_opposite_side_addition(self) -> None:
        frame = _multi_asset_frame(opens={"A": [100.0] * 6})
        timestamps = frame.index.get_level_values("datetime").unique()
        frame["volume"] = 100.0
        frame.loc[("A", timestamps[1]), "volume"] = 1.0
        frame.loc[("A", timestamps[2]), "volume"] = 3.0

        class Reverse(Strategy):
            def on_bar(self, ctx: Context) -> StrategyDecision:
                if ctx.period_index == 0:
                    return PortfolioWeights(weights={"A": 0.2}, reason="seed")
                if ctx.period_index == 1:
                    return PortfolioWeights(weights={"A": -0.2}, reason="reverse")
                return []

        result = Backtest(
            frame,
            Reverse(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=1.0,
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        ).run()

        reversal_events = [event for event in result.position_events if event.reason == "reverse"]
        assert [(event.ts, event.event_type, event.fill_quantity) for event in reversal_events] == [
            (timestamps[2], "reduce", pytest.approx(1.0)),
            (timestamps[3], "close", pytest.approx(1.0)),
            (timestamps[3], "open", pytest.approx(2.0)),
        ]

    def test_position_snapshots_include_realized_weights(self) -> None:
        frame = _multi_asset_frame(
            opens={
                "A": [100.0, 100.0, 100.0, 100.0, 100.0],
                "B": [200.0, 200.0, 200.0, 200.0, 200.0],
            }
        )
        backtest = Backtest(
            frame,
            OneRebalance(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
            record_position_snapshots=True,
        )
        backtest.run()
        output = backtest.build_output()

        first_snapshot_ts = frame.index.get_level_values("datetime").unique()[1]
        snapshots = [
            snapshot for snapshot in output.position_snapshots if snapshot.ts == first_snapshot_ts
        ]
        assert [snapshot.symbol for snapshot in snapshots] == ["A", "B"]
        assert all(np.isclose(snapshot.realized_weight, 0.5) for snapshot in snapshots)
        assert all(snapshot.market_value > 0 for snapshot in snapshots)

        allocations = [
            snapshot for snapshot in output.allocation_snapshots if snapshot.ts == first_snapshot_ts
        ]
        assert [snapshot.symbol for snapshot in allocations] == ["A", "B"]
        assert all(np.isclose(snapshot.target_weight, 0.5) for snapshot in allocations)
        assert all(np.isclose(snapshot.realized_weight, 0.5) for snapshot in allocations)
        assert all(np.isclose(snapshot.weight_drift, 0.0) for snapshot in allocations)

        point = next(point for point in output.equity_curve if point.ts == first_snapshot_ts)
        assert point.gross_exposure == pytest.approx(1.0)
        assert point.net_exposure == pytest.approx(1.0)
        assert point.concentration == pytest.approx(0.5)
        assert point.turnover == pytest.approx(1.0)
        assert output.metrics.total_turnover is not None
        assert output.metrics.max_gross_exposure == pytest.approx(1.0)
