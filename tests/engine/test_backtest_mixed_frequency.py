"""Point-in-time mixed-frequency backtest regressions."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, MarketDataSubscription, build_backtest_artifact
from librae.core import market_data as market_data_module
from librae.core.cost_model import CostModel
from librae.core.executor import REASON_DRAWDOWN_BREACH, REASON_STOP_LOSS
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import Context, OrderIntent, PortfolioWeights, Strategy
from librae.core.trading_calendar import period_close
from librae.db.timescale_writer import save_backtest_output

from tests.conftest import make_test_cfg


def _subscription(
    symbol: str,
    timeframe: str,
    *,
    calendar_id: str = "24/7",
    session_mode: str = "extended",
    source: str = "fixture",
) -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source=source,
        instrument_type="spot",
    )


def _frame(
    symbol: str,
    timestamps: list[str] | pd.DatetimeIndex,
    *,
    available_at: list[str] | pd.DatetimeIndex | None = None,
    feature: list[object] | None = None,
    multiindex: bool = True,
    volume: float = 100.0,
) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True), name="datetime")
    values = np.arange(len(index), dtype=float) + 100.0
    data: dict[str, object] = {
        "open": values,
        "high": values + 1.0,
        "low": values - 1.0,
        "close": values + 0.5,
        "volume": np.full(len(index), volume),
    }
    if available_at is not None:
        data["available_at"] = pd.DatetimeIndex(pd.to_datetime(available_at, utc=True))
    if feature is not None:
        data["feature"] = feature
    frame = pd.DataFrame(data, index=index)
    if multiindex:
        frame["symbol"] = symbol
        frame = frame.set_index("symbol", append=True).reorder_levels(["symbol", "datetime"])
    return frame


class _Capture(Strategy):
    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.contexts.append(ctx)
        return []


def _mixed_backtest(
    primary: pd.DataFrame,
    strategy: Strategy,
    auxiliary: dict[MarketDataSubscription, pd.DataFrame],
    *,
    primary_subscriptions: tuple[MarketDataSubscription, ...] | None = None,
) -> Backtest:
    symbols = tuple(primary.index.get_level_values("symbol").unique())
    subscriptions = primary_subscriptions or tuple(
        _subscription(symbol, "H1") for symbol in symbols
    )
    return Backtest(
        primary,
        strategy,
        strategy_name="capture",
        cost_model=CostModel.zero(),
        primary_subscriptions=subscriptions,
        auxiliary_data=auxiliary,
    )


def test_auxiliary_history_uses_inclusive_asof_and_canonical_time_order() -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("PRIMARY", "D1", source="daily")
    auxiliary = _frame(
        "PRIMARY",
        ["2026-01-01 00:00Z", "2026-01-02 00:00Z"],
        available_at=["2026-01-03 02:00Z", "2026-01-03 01:00Z"],
        feature=[1.0, 2.0],
        multiindex=False,
    )
    strategy = _Capture()

    _mixed_backtest(primary, strategy, {auxiliary_subscription: auxiliary}).run()

    first, second = strategy.contexts[:2]
    assert first.decision_at == pd.Timestamp("2026-01-03 01:00Z")
    assert second.decision_at == pd.Timestamp("2026-01-03 02:00Z")
    assert first.market_data is not None
    assert first.market_data.history(auxiliary_subscription)["feature"].tolist() == [2.0]
    history = second.market_data.history(auxiliary_subscription)
    assert history["feature"].tolist() == [1.0, 2.0]
    assert list(history.index) == sorted(history.index)
    assert "available_at" in history


def test_frontier_is_cumulative_but_primary_history_only_commits_processed_cohorts() -> None:
    primary_subscription = _subscription("PRIMARY", "H1")
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
        available_at=[
            "2026-01-03 10:00Z",
            "2026-01-03 02:00Z",
            "2026-01-03 03:00Z",
            "2026-01-03 04:00Z",
            "2026-01-03 05:00Z",
        ],
    )
    aux_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame(
        "AUX",
        ["2026-01-01 00:00Z"],
        available_at=["2026-01-02 00:00Z"],
        multiindex=False,
    )
    strategy = _Capture()

    _mixed_backtest(
        primary,
        strategy,
        {aux_subscription: auxiliary},
        primary_subscriptions=(primary_subscription,),
    ).run()

    assert [ctx.decision_at for ctx in strategy.contexts] == [pd.Timestamp("2026-01-03 10:00Z")] * 5
    assert strategy.contexts[0].market_data is not None
    assert len(strategy.contexts[0].market_data.history(primary_subscription)) == 1
    assert len(strategy.contexts[1].market_data.history(primary_subscription)) == 2


def test_view_contains_no_future_rows_and_mutation_cannot_affect_later_context() -> None:
    primary_subscription = _subscription("PRIMARY", "H1")
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame(
        "AUX",
        ["2026-01-01 00:00Z", "2026-01-02 00:00Z"],
        available_at=["2026-01-03 01:00Z", "2026-01-03 03:00Z"],
        feature=[1.0, 2.0],
        multiindex=False,
    )

    class MutatingStrategy(Strategy):
        def __init__(self) -> None:
            self.lengths: list[int] = []
            self.values: list[float] = []

        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            assert ctx.market_data is not None
            returned = ctx.market_data.history(auxiliary_subscription)
            self.lengths.append(len(returned))
            if not returned.empty:
                self.values.append(float(returned.iloc[0]["feature"]))
                returned.iloc[0, returned.columns.get_loc("feature")] = 999.0
            return []

    strategy = MutatingStrategy()
    _mixed_backtest(
        primary,
        strategy,
        {auxiliary_subscription: auxiliary},
        primary_subscriptions=(primary_subscription,),
    ).run()

    assert strategy.lengths == [1, 1, 2, 2, 2]
    assert strategy.values == [1.0] * 5


def test_object_feature_cells_are_detached_from_caller_and_history_results() -> None:
    primary_subscription = _subscription("PRIMARY", "H1")
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
        feature=[{"score": value} for value in range(5)],
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame(
        "AUX",
        ["2026-01-01"],
        feature=[{"score": 10}],
        multiindex=False,
    )

    class MutateNestedValues(Strategy):
        def __init__(self) -> None:
            self.values: list[tuple[int, int]] = []

        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            assert ctx.market_data is not None
            primary_history = ctx.market_data.history(primary_subscription)
            auxiliary_history = ctx.market_data.history(auxiliary_subscription)
            primary_feature = primary_history.iloc[0]["feature"]
            auxiliary_feature = auxiliary_history.iloc[0]["feature"]
            self.values.append((primary_feature["score"], auxiliary_feature["score"]))
            primary_feature["score"] = 999
            auxiliary_feature["score"] = 999
            return []

    strategy = MutateNestedValues()
    backtest = _mixed_backtest(
        primary,
        strategy,
        {auxiliary_subscription: auxiliary},
        primary_subscriptions=(primary_subscription,),
    )
    primary.iloc[0, primary.columns.get_loc("feature")]["score"] = 111
    auxiliary.iloc[0, auxiliary.columns.get_loc("feature")]["score"] = 222

    backtest.run()

    assert strategy.values == [(0, 10)] * 5


def test_auxiliary_rows_never_create_primary_runtime_events() -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame(
        "AUX",
        ["2025-12-30 00:00Z", "2025-12-31 00:00Z", "2026-01-01 00:00Z"],
        multiindex=False,
    )
    strategy = _Capture()

    result = _mixed_backtest(primary, strategy, {auxiliary_subscription: auxiliary}).run()

    assert len(strategy.contexts) == len(primary)
    assert len(result.equity_curve) == len(primary)
    assert not result.position_events
    assert all(ctx.available_symbols == ("PRIMARY",) for ctx in strategy.contexts)
    assert all("available_at" not in ctx.bar for ctx in strategy.contexts)


def test_independent_pending_intents_keep_their_emission_frontiers() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=7, freq="h", tz="UTC")
    primary_a = _frame(
        "A",
        timestamps,
        available_at=pd.DatetimeIndex(
            [
                "2026-01-03 01:00Z",
                "2026-01-03 04:00Z",
                "2026-01-03 03:00Z",
                "2026-01-03 04:00Z",
                "2026-01-03 05:00Z",
                "2026-01-03 06:00Z",
                "2026-01-03 07:00Z",
            ]
        ),
    )
    primary_b = _frame("B", timestamps)
    primary = pd.concat([primary_a, primary_b])
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame("AUX", ["2026-01-01"], multiindex=False)

    class TwoEmissions(Strategy):
        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            if ctx.period_index == 0:
                return [OrderIntent("long", symbol="A", quantity=1.0)]
            if ctx.period_index == 1:
                return [OrderIntent("long", symbol="B", quantity=1.0)]
            return []

    result = _mixed_backtest(
        primary,
        TwoEmissions(),
        {auxiliary_subscription: auxiliary},
    ).run()
    opens = [event for event in result.position_events if event.event_type == "open"]

    assert [(event.symbol, event.ts) for event in opens] == [
        ("A", pd.Timestamp("2026-01-03 02:00Z")),
        ("B", pd.Timestamp("2026-01-03 05:00Z")),
    ]


def test_pending_group_id_cannot_be_reused_across_emissions() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC")
    late_available = pd.DatetimeIndex(
        [
            "2026-01-03 04:00Z",
            "2026-01-03 02:00Z",
            "2026-01-03 03:00Z",
            "2026-01-03 04:00Z",
            "2026-01-03 05:00Z",
        ]
    )
    primary = pd.concat(
        [
            _frame("A", timestamps, available_at=late_available),
            _frame("B", timestamps),
        ]
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    class ReusedGroup(Strategy):
        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            if ctx.period_index == 0:
                return [OrderIntent("long", symbol="A", quantity=1.0, group_id="spread")]
            if ctx.period_index == 1:
                return [OrderIntent("long", symbol="B", quantity=2_000.0, group_id="spread")]
            return []

    with pytest.raises(ValueError, match="reused pending group_id"):
        _mixed_backtest(
            primary,
            ReusedGroup(),
            {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        ).run()


@pytest.mark.parametrize("portfolio", [False, True])
def test_group_and_portfolio_emissions_require_strictly_later_bar_start(
    portfolio: bool,
) -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC")
    primary = pd.concat([_frame("A", timestamps), _frame("B", timestamps)])
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = _frame("AUX", ["2026-01-01"], multiindex=False)

    class EmitOnce(Strategy):
        def on_bar(self, ctx: Context):
            if ctx.period_index:
                return []
            if portfolio:
                return PortfolioWeights({"A": 0.25, "B": 0.25})
            return [
                OrderIntent("long", symbol="A", quantity=1.0, group_id="pair"),
                OrderIntent("long", symbol="B", quantity=1.0, group_id="pair"),
            ]

    result = _mixed_backtest(primary, EmitOnce(), {auxiliary_subscription: auxiliary}).run()
    opens = [event for event in result.position_events if event.event_type == "open"]

    assert {event.ts for event in opens} == {pd.Timestamp("2026-01-03 02:00Z")}


def test_activated_rebalance_residual_is_not_regated_by_later_frontier() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=6, freq="h", tz="UTC")
    primary = _frame(
        "PRIMARY",
        timestamps,
        available_at=[
            "2026-01-03 01:00Z",
            "2026-01-03 02:00Z",
            "2026-01-03 03:00Z",
            "2026-01-03 10:00Z",
            "2026-01-03 05:00Z",
            "2026-01-03 06:00Z",
        ],
    )
    primary_subscription = _subscription("PRIMARY", "H1")
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    class TargetOnce(Strategy):
        def on_bar(self, ctx: Context):
            return PortfolioWeights({"PRIMARY": 0.002}) if ctx.period_index == 0 else []

    result = Backtest(
        primary,
        TargetOnce(),
        cost_model=CostModel.zero(),
        primary_subscriptions=(primary_subscription,),
        auxiliary_data={auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        execution=ExecutionPolicy(
            max_bar_volume_participation_rate=0.01,
            rebalance_residual_policy="defer_symbols",
            max_rebalance_delay_bars=4,
        ),
    ).run()
    additions = [event for event in result.position_events if event.event_type in ("open", "add")]

    assert [event.ts for event in additions] == [
        pd.Timestamp("2026-01-03 02:00Z"),
        pd.Timestamp("2026-01-03 03:00Z"),
    ]


def test_activated_protection_is_not_regated_by_later_frontier() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=6, freq="h", tz="UTC")
    primary = _frame(
        "PRIMARY",
        timestamps,
        available_at=[
            "2026-01-03 01:00Z",
            "2026-01-03 02:00Z",
            "2026-01-03 03:00Z",
            "2026-01-03 10:00Z",
            "2026-01-03 20:00Z",
            "2026-01-03 06:00Z",
        ],
    )
    primary.loc[("PRIMARY", timestamps[2]), "volume"] = 50.0
    primary.loc[("PRIMARY", timestamps[3]), "low"] = 90.0
    primary_subscription = _subscription("PRIMARY", "H1")
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    class ProtectedEntry(Strategy):
        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            if ctx.period_index == 0:
                return [
                    OrderIntent(
                        "long",
                        symbol="PRIMARY",
                        quantity=2.0,
                        stop_price=95.0,
                    )
                ]
            return []

    result = Backtest(
        primary,
        ProtectedEntry(),
        cost_model=CostModel.zero(),
        primary_subscriptions=(primary_subscription,),
        auxiliary_data={auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        execution=ExecutionPolicy(max_bar_volume_participation_rate=0.02),
    ).run()
    stops = [event for event in result.position_events if event.reason == REASON_STOP_LOSS]

    assert [event.ts for event in stops] == [
        pd.Timestamp("2026-01-03 03:00Z"),
        pd.Timestamp("2026-01-03 04:00Z"),
    ]


def test_drawdown_exit_uses_breach_frontier_then_remains_activated() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=13, freq="h", tz="UTC")
    available = timestamps + pd.Timedelta(hours=1)
    available = available.to_series(index=range(len(timestamps)))
    available.iloc[2] = pd.Timestamp("2026-01-03 10:00Z")
    primary = _frame(
        "PRIMARY",
        timestamps,
        available_at=pd.DatetimeIndex(available),
    )
    primary.loc[("PRIMARY", timestamps[2]), ["low", "close"]] = [49.0, 50.0]
    primary.loc[("PRIMARY", timestamps[3:]), ["open", "high", "low", "close"]] = [
        50.0,
        51.0,
        49.0,
        50.0,
    ]
    primary_subscription = _subscription("PRIMARY", "H1")
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    class EnterOnce(Strategy):
        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            if ctx.period_index == 0:
                return [OrderIntent("long", symbol="PRIMARY", quantity=900.0)]
            return []

    result = Backtest(
        primary,
        EnterOnce(),
        initial_balance=1_100.0,
        cost_model=CostModel.zero(),
        primary_subscriptions=(primary_subscription,),
        auxiliary_data={auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        risk=RiskPolicy(max_drawdown_rate=0.1),
    ).run()
    exits = [event for event in result.position_events if event.reason == REASON_DRAWDOWN_BREACH]

    assert len(exits) == 1
    assert exits[0].ts == pd.Timestamp("2026-01-03 11:00Z")


def test_shuffled_inputs_and_mapping_order_have_identical_views() -> None:
    primary_subscription = _subscription("PRIMARY", "H1")
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    daily = _subscription("AUX", "D1", source="daily")
    weekly = _subscription("AUX", "W1", source="weekly")
    daily_frame = _frame(
        "AUX",
        ["2025-12-31", "2026-01-01"],
        multiindex=False,
    )
    weekly_frame = _frame("AUX", ["2025-12-29"], multiindex=False)

    def observed(
        primary_frame: pd.DataFrame,
        auxiliary: dict[MarketDataSubscription, pd.DataFrame],
    ) -> list[tuple[datetime, tuple[tuple[str, int], ...]]]:
        strategy = _Capture()
        _mixed_backtest(
            primary_frame,
            strategy,
            auxiliary,
            primary_subscriptions=(primary_subscription,),
        ).run()
        return [
            (
                ctx.decision_at,
                tuple(
                    (subscription.timeframe, len(ctx.market_data.history(subscription)))
                    for subscription in ctx.market_data.subscriptions
                ),
            )
            for ctx in strategy.contexts
            if ctx.market_data is not None and ctx.decision_at is not None
        ]

    canonical = observed(primary, {daily: daily_frame, weekly: weekly_frame})
    shuffled = observed(
        primary.iloc[::-1],
        {weekly: weekly_frame.iloc[::-1], daily: daily_frame.iloc[::-1]},
    )

    assert shuffled == canonical


def test_reversed_multiasset_rows_use_declared_primary_subscription_order() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC")
    canonical_primary = pd.concat([_frame("A", timestamps), _frame("B", timestamps)])
    primary_subscriptions = (_subscription("A", "H1"), _subscription("B", "H1"))
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    auxiliary = {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)}

    canonical_strategy = _Capture()
    reversed_strategy = _Capture()
    canonical = _mixed_backtest(
        canonical_primary,
        canonical_strategy,
        auxiliary,
        primary_subscriptions=primary_subscriptions,
    )
    reversed_run = _mixed_backtest(
        canonical_primary.iloc[::-1],
        reversed_strategy,
        auxiliary,
        primary_subscriptions=primary_subscriptions,
    )

    assert reversed_run.run() == canonical.run()
    assert reversed_run.build_output().run_metadata.symbols == ("A", "B")
    assert [ctx.available_symbols for ctx in reversed_strategy.contexts] == [
        ctx.available_symbols for ctx in canonical_strategy.contexts
    ]


def test_empty_auxiliary_mapping_preserves_legacy_context_and_result() -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    none_strategy = _Capture()
    empty_strategy = _Capture()

    none_result = Backtest(primary, none_strategy, cost_model=CostModel.zero()).run()
    empty_result = Backtest(
        primary,
        empty_strategy,
        cost_model=CostModel.zero(),
        auxiliary_data={},
    ).run()

    assert none_result == empty_result
    assert all(
        ctx.decision_at is None and ctx.market_data is None for ctx in none_strategy.contexts
    )
    assert all(
        ctx.decision_at is None and ctx.market_data is None for ctx in empty_strategy.contexts
    )


def test_direct_mixed_backtest_requires_full_primary_identities() -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    with pytest.raises(ValueError, match="requires primary_subscriptions"):
        Backtest(
            primary,
            _Capture(),
            cost_model=CostModel.zero(),
            auxiliary_data={
                auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)
            },
        )


def test_direct_mixed_backtest_uses_primary_identity_order_as_ssot() -> None:
    timestamps = pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC")
    primary = pd.concat([_frame("A", timestamps), _frame("B", timestamps)])
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    backtest = _mixed_backtest(
        primary,
        _Capture(),
        {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        primary_subscriptions=(_subscription("B", "H1"), _subscription("A", "H1")),
    )

    backtest.run()

    assert backtest.build_output().run_metadata.symbols == ("B", "A")


def test_direct_daily_identity_accepts_sparse_weekdays_across_dst() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-02-23 14:30Z",
                "2026-03-02 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-16 13:30Z",
                "2026-03-23 13:30Z",
            ],
            utc=True,
        )
    )
    primary = _frame("MU", timestamps)
    primary_subscription = _subscription(
        "MU",
        "D1",
        calendar_id="XNYS",
        session_mode="regular",
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    strategy = _Capture()

    backtest = _mixed_backtest(
        primary,
        strategy,
        {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
        primary_subscriptions=(primary_subscription,),
    )
    backtest.run()

    assert backtest.build_output().run_metadata.timeframe == "D1"
    assert len(strategy.contexts) == 5


def test_direct_weekly_identity_rejects_daily_session_rows() -> None:
    timestamps = pd.DatetimeIndex(
        pd.to_datetime(
            [
                "2026-03-02 14:30Z",
                "2026-03-03 14:30Z",
                "2026-03-04 14:30Z",
                "2026-03-05 14:30Z",
                "2026-03-06 14:30Z",
            ],
            utc=True,
        )
    )
    primary = _frame("MU", timestamps)
    primary_subscription = _subscription(
        "MU",
        "W1",
        calendar_id="XNYS",
        session_mode="regular",
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")

    with pytest.raises(ValueError, match="canonical timeframe=W1 period starts"):
        _mixed_backtest(
            primary,
            _Capture(),
            {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
            primary_subscriptions=(primary_subscription,),
        ).run()


def test_market_data_view_materializes_history_only_on_demand(monkeypatch) -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=10, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    strategy = _Capture()
    calls = 0
    original = market_data_module._MarketDataSource.history

    def counted_history(self, subscription, visible_count, limit):
        nonlocal calls
        calls += 1
        return original(self, subscription, visible_count, limit)

    monkeypatch.setattr(market_data_module._MarketDataSource, "history", counted_history)
    _mixed_backtest(
        primary,
        strategy,
        {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
    ).run()

    assert calls == 0
    assert len({id(ctx.market_data._source) for ctx in strategy.contexts}) == 1
    assert all(isinstance(ctx.market_data._visible_counts, tuple) for ctx in strategy.contexts)
    strategy.contexts[-1].market_data.history(auxiliary_subscription)
    assert calls == 1


def test_config_mixed_backtest_reuses_resolved_primary_identity() -> None:
    primary = _frame(
        "BTCUSDT",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("BTCUSDT", "D1", source="daily")
    strategy = _Capture()
    config = make_test_cfg(mode="backtest", data_source="fixture")

    backtest = Backtest(
        primary,
        strategy,
        config=config,
        cost_model=CostModel.zero(),
        auxiliary_data={
            auxiliary_subscription: _frame("BTCUSDT", ["2026-01-01"], multiindex=False)
        },
    )
    backtest.run()

    assert backtest.primary_subscriptions[0] in strategy.contexts[0].market_data.subscriptions
    assert backtest.auxiliary_subscriptions == (auxiliary_subscription,)


def test_context_and_view_snapshots_are_metadata_safe() -> None:
    primary = _frame(
        "PRIMARY",
        pd.date_range("2026-01-03 00:00", periods=5, freq="h", tz="UTC"),
    )
    auxiliary_subscription = _subscription("AUX", "D1", source="daily")
    strategy = _Capture()
    backtest = _mixed_backtest(
        primary,
        strategy,
        {auxiliary_subscription: _frame("AUX", ["2026-01-01"], multiindex=False)},
    )

    backtest.run()
    output = backtest.build_output()

    assert output.run_metadata.auxiliary_subscriptions == (auxiliary_subscription,)
    assert output.to_dict()["run_metadata"]["auxiliary_subscriptions"] == [
        auxiliary_subscription.to_dict()
    ]
    artifact = build_backtest_artifact(output, config_hash="mixed-config")
    assert artifact.manifest["artifact_schema_version"] == 4
    assert artifact.manifest["run_metadata"]["auxiliary_subscriptions"] == [
        auxiliary_subscription.to_dict()
    ]
    with pytest.raises(ValueError, match="does not yet store auxiliary_subscriptions"):
        save_backtest_output(output)
    assert (
        asdict(strategy.contexts[0].market_data)["as_of"]
        == pd.Timestamp("2026-01-03 01:00Z").to_pydatetime()
    )


@pytest.mark.parametrize(
    ("timeframe", "timestamps"),
    [
        ("D1", ["2026-03-06 14:30Z", "2026-03-09 13:30Z"]),
        ("W1", ["2026-03-02 14:30Z", "2026-03-09 13:30Z"]),
        ("MN1", ["2026-03-02 14:30Z", "2026-04-01 13:30Z"]),
    ],
)
def test_calendar_sized_auxiliary_history_accepts_canonical_dst_periods(
    timeframe: str,
    timestamps: list[str],
) -> None:
    subscription = _subscription(
        "MU",
        timeframe,
        calendar_id="XNYS",
        session_mode="regular",
        source="daily",
    )
    index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    available = pd.DatetimeIndex([period_close(ts, timeframe, "XNYS") for ts in index])
    auxiliary = _frame(
        "MU",
        index,
        available_at=available,
        multiindex=False,
    )
    frontier = max(available) + pd.Timedelta(days=1)
    primary = _frame(
        "PRIMARY",
        pd.date_range(frontier - pd.Timedelta(hours=4), periods=5, freq="h"),
    )
    strategy = _Capture()

    _mixed_backtest(primary, strategy, {subscription: auxiliary}).run()

    assert strategy.contexts[-1].market_data is not None
    assert len(strategy.contexts[-1].market_data.history(subscription)) == 2
