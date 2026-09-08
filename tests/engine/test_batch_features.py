"""Shared point-in-time batch feature regressions."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, FeatureBatch, MarketDataSubscription
from librae.core.cost_model import CostModel
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader

from tests.conftest import make_test_cfg


def _subscription(symbol: str, timeframe: str = "H1") -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id="24/7",
        session_mode="extended",
        data_source="fixture",
        instrument_type="spot",
    )


def _primary_frame(
    symbol: str,
    timestamps: pd.DatetimeIndex,
    closes: list[float],
    *,
    available_at: list[str],
) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "open": np.asarray(closes) - 0.5,
            "high": np.asarray(closes) + 1.0,
            "low": np.asarray(closes) - 1.0,
            "close": closes,
            "volume": 1_000.0,
            "available_at": pd.to_datetime(available_at, utc=True),
            "symbol": symbol,
        },
        index=timestamps,
    )
    frame.index.name = "datetime"
    return frame.set_index("symbol", append=True).reorder_levels(["symbol", "datetime"])


def _auxiliary_frame() -> pd.DataFrame:
    index = pd.DatetimeIndex(
        pd.to_datetime(["2025-12-30T00:00Z", "2025-12-31T00:00Z"], utc=True),
        name="datetime",
    )
    return pd.DataFrame(
        {
            "open": [1.0, 2.0],
            "high": [1.0, 2.0],
            "low": [1.0, 2.0],
            "close": [1.0, 2.0],
            "volume": [1.0, 1.0],
            "available_at": pd.to_datetime(["2026-01-01T09:00Z", "2026-01-01T11:00Z"], utc=True),
        },
        index=index,
    )


class _Capture(Strategy):
    def __init__(self) -> None:
        self.contexts: list[Context] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.contexts.append(ctx)
        return []


def test_backtest_batch_features_use_one_ordered_snapshot_per_cohort() -> None:
    timestamps = pd.date_range("2026-01-01T00:00Z", periods=3, freq="h")
    availability = ["2026-01-01T10:00Z"] * 3
    primary = pd.concat(
        [
            _primary_frame("B", timestamps, [20.0, 21.0, 22.0], available_at=availability),
            _primary_frame("A", timestamps, [10.0, 11.0, 12.0], available_at=availability),
        ]
    )
    subscriptions = (_subscription("A"), _subscription("B"))
    auxiliary = _subscription("MACRO", "D1")
    batches: list[
        tuple[
            datetime,
            datetime,
            tuple[MarketDataSubscription, ...],
            tuple[int, ...],
            int,
        ]
    ] = []

    def features(batch: FeatureBatch):
        lengths = tuple(
            len(batch.market_data.history(subscription))
            for subscription in batch.primary_subscriptions
        )
        batches.append(
            (
                batch.event_ts,
                batch.as_of,
                batch.active_primary_subscriptions,
                lengths,
                len(batch.market_data.history(auxiliary)),
            )
        )
        latest = {
            subscription: float(batch.market_data.history(subscription)["close"].iloc[-1])
            for subscription in batch.active_primary_subscriptions
        }
        spread = latest[subscriptions[0]] - latest[subscriptions[1]]
        output = {}
        for subscription in reversed(batch.active_primary_subscriptions):
            frame = batch.market_data.history(subscription)
            frame["spread"] = spread
            output[subscription] = frame
        return output

    strategy = _Capture()
    Backtest(
        primary,
        strategy,
        cost_model=CostModel.zero(),
        primary_subscriptions=subscriptions,
        auxiliary_data={auxiliary: _auxiliary_frame()},
        batch_feature_fn=features,
    ).run()

    expected_as_of = pd.Timestamp("2026-01-01T10:00Z").to_pydatetime()
    assert [item[0] for item in batches] == list(timestamps.to_pydatetime())
    assert [item[1] for item in batches] == [expected_as_of] * 3
    assert [item[2] for item in batches] == [subscriptions] * 3
    assert [item[3] for item in batches] == [(1, 1), (2, 2), (3, 3)]
    assert [item[4] for item in batches] == [1, 1, 1]
    assert [ctx.available_symbols for ctx in strategy.contexts] == [("A", "B")] * 3
    assert [ctx.bars["A"]["spread"] for ctx in strategy.contexts] == [-10.0] * 3


def test_backtest_batch_output_is_validated_before_strategy_publication() -> None:
    timestamps = pd.date_range("2026-01-01T00:00Z", periods=2, freq="h")
    availability = ["2026-01-01T01:00Z", "2026-01-01T02:00Z"]
    primary = pd.concat(
        [
            _primary_frame("A", timestamps, [10.0, 11.0], available_at=availability),
            _primary_frame("B", timestamps, [20.0, 21.0], available_at=availability),
        ]
    )
    subscriptions = (_subscription("A"), _subscription("B"))
    strategy = _Capture()

    def missing_symbol(batch: FeatureBatch):
        return {
            subscriptions[0]: batch.market_data.history(subscriptions[0]),
        }

    backtest = Backtest(
        primary,
        strategy,
        cost_model=CostModel.zero(),
        primary_subscriptions=subscriptions,
        batch_feature_fn=missing_symbol,
    )

    with pytest.raises(ValueError, match="exactly cover active primary subscriptions"):
        backtest.run()

    assert strategy.contexts == []


def test_direct_batch_backtest_requires_explicit_primary_identities() -> None:
    timestamps = pd.date_range("2026-01-01T00:00Z", periods=2, freq="h")
    primary = _primary_frame(
        "A",
        timestamps,
        [10.0, 11.0],
        available_at=["2026-01-01T01:00Z", "2026-01-01T02:00Z"],
    )

    with pytest.raises(ValueError, match="batch_feature_fn requires primary_subscriptions"):
        Backtest(
            primary,
            _Capture(),
            cost_model=CostModel.zero(),
            batch_feature_fn=lambda _batch: {},
        )


def test_backtest_and_live_share_the_configured_batch_history_window() -> None:
    timestamps = pd.date_range("2026-01-01T00:00Z", periods=5, freq="h")
    available_at = list((timestamps + pd.Timedelta(hours=1)).astype(str))
    primary = pd.concat(
        [
            _primary_frame(
                "A", timestamps, [10.0, 11.0, 12.0, 13.0, 14.0], available_at=available_at
            ),
            _primary_frame(
                "B", timestamps, [20.0, 21.0, 22.0, 23.0, 24.0], available_at=available_at
            ),
        ]
    )
    instrument_overrides = {
        symbol: {
            "instrument_type": "spot",
            "currency": "USDT",
            "calendar_id": "24/7",
        }
        for symbol in ("A", "B")
    }
    execution = ExecutionPolicy(
        max_bar_volume_participation_rate=None,
        warmup_periods=2,
    )
    observed: dict[
        str,
        tuple[
            tuple[tuple[str, tuple[pd.Timestamp, ...]], ...],
            tuple[tuple[str, tuple[pd.Timestamp, ...]], ...],
            tuple[tuple[str, tuple[pd.Timestamp, ...]], ...],
        ],
    ] = {}
    observed_auxiliary: list[tuple[pd.Timestamp, ...]] = []
    final_ts = timestamps[-1].to_pydatetime()
    auxiliary_subscription = _subscription("MACRO", "D1")
    auxiliary_index = pd.date_range("2025-12-27T00:00Z", periods=4, freq="D", name="datetime")
    auxiliary_data = pd.DataFrame(
        {
            "open": [1.0, 2.0, 3.0, 4.0],
            "high": [1.0, 2.0, 3.0, 4.0],
            "low": [1.0, 2.0, 3.0, 4.0],
            "close": [1.0, 2.0, 3.0, 4.0],
            "volume": 1.0,
            "available_at": auxiliary_index + pd.Timedelta(days=1),
        },
        index=auxiliary_index,
    )

    def features(label: str):
        def evaluate(batch: FeatureBatch):
            if batch.event_ts == final_ts:
                observed[label] = tuple(
                    tuple(
                        (
                            subscription.symbol,
                            tuple(batch.market_data.history(subscription, limit=limit).index),
                        )
                        for subscription in batch.primary_subscriptions
                    )
                    for limit in (None, 1, 99)
                )
                if label == "backtest":
                    observed_auxiliary.append(
                        tuple(batch.market_data.history(auxiliary_subscription).index)
                    )
            return {
                subscription: batch.market_data.history(subscription)
                for subscription in batch.active_primary_subscriptions
            }

        return evaluate

    backtest_config = make_test_cfg(
        mode="backtest",
        symbols=["A", "B"],
        instrument_overrides=instrument_overrides,
        symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in ("A", "B")},
        execution=execution,
    )
    Backtest(
        primary,
        _Capture(),
        config=backtest_config,
        cost_model=CostModel.zero(),
        auxiliary_data={auxiliary_subscription: auxiliary_data},
        batch_feature_fn=features("backtest"),
    ).run()

    live_frames = {
        symbol: primary.xs(symbol, level="symbol").reset_index(names="ts") for symbol in ("A", "B")
    }
    live_config = make_test_cfg(
        mode="sim",
        symbols=["A", "B"],
        instrument_overrides=instrument_overrides,
        symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in ("A", "B")},
        execution=execution,
    )
    trader = LiveTrader(
        _Capture(),
        None,
        config=live_config,
        batch_feature_fn=features("live"),
        adapter={
            symbol: (lambda *_args, frame=frame, **_kwargs: frame)
            for symbol, frame in live_frames.items()
        },
        cost_model=CostModel.zero(),
        clock=lambda: datetime(2026, 1, 1, 6, tzinfo=UTC),
    )
    trader.run(max_iterations=1)

    assert observed["live"] == observed["backtest"]
    no_limit, one_row, oversized_limit = observed["backtest"]
    assert all(index == tuple(timestamps[-2:]) for _, index in no_limit)
    assert all(index == (timestamps[-1],) for _, index in one_row)
    assert oversized_limit == no_limit
    assert observed_auxiliary == [tuple(auxiliary_index[-2:])]
