"""Portfolio target-rebalance execution and snapshot tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import ExecutionResult, execute_portfolio_targets
from librae.core.strategy import (
    Context,
    OrderIntent,
    PortfolioTargets,
    PositionState,
    Strategy,
    StrategyDecision,
)

TS = datetime(2026, 7, 27, tzinfo=UTC)


def _process(
    targets: PortfolioTargets,
    positions: dict[str, PositionState],
    cash: float,
    *,
    prices: dict[str, float],
    cost_model: CostModel | None = None,
) -> ExecutionResult:
    model = cost_model or CostModel.zero()
    return execute_portfolio_targets(
        targets,
        positions,
        cash,
        TS,
        get_price=lambda symbol, _action: prices.get(symbol),
        get_cost_model=lambda _symbol: model,
        primary_symbol="A",
    )


def _multi_asset_frame(
    opens: dict[str, list[float]],
    closes: dict[str, list[float]] | None = None,
) -> pd.DataFrame:
    timestamps = pd.date_range(
        "2026-01-01",
        periods=len(next(iter(opens.values()))),
        freq="h",
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
            return PortfolioTargets(weights={"A": 0.5, "B": 0.5})
        return []


class TestPortfolioTargetsValidation:
    def test_rejects_non_finite_weight(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            PortfolioTargets(weights={"A": float("nan")})

    def test_weights_are_immutable_after_validation(self) -> None:
        targets = PortfolioTargets(weights={"A": 1.0})

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
            PortfolioTargets(weights={"B": 0.5, "A": 0.5}),
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
            PortfolioTargets(weights={"A": 0.5, "B": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
        )
        cash = 1_000.0 + first.cash_delta

        second = _process(
            PortfolioTargets(weights={"A": 0.25, "B": 0.75}),
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
            PortfolioTargets(weights={"A": 0.5, "B": 0.5}),
            positions,
            1_000.0,
            prices={"A": 100.0, "B": 50.0},
        )
        cash = 1_000.0 + first.cash_delta

        second = _process(
            PortfolioTargets(weights={"B": 1.0}),
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
            PortfolioTargets(weights={"A": 0.5, "B": 0.5}),
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

    def test_weight_remainder_stays_in_cash(self) -> None:
        positions: dict[str, PositionState] = {}
        result = _process(
            PortfolioTargets(weights={"A": 0.95}),
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
                PortfolioTargets(weights={"A": 0.5, "B": 0.5}),
                positions,
                1_000.0,
                prices={"A": 100.0},
            )

        assert positions == {}


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
        open_events = [event for event in result.order_events if event.event_type == "open"]

        assert strategy.seen_equity[0] == 1_000.0
        assert [event.price for event in open_events] == [120.0, 240.0]
        assert np.isclose(open_events[0].fill_quantity, 1_000.0 * 0.5 / 120.0)
        assert np.isclose(open_events[1].fill_quantity, 1_000.0 * 0.5 / 240.0)
        assert all(
            event.ts == frame.index.get_level_values("datetime").unique()[1]
            for event in open_events
        )

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
