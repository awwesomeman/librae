"""Behavior tests for the runnable portfolio examples."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from examples.minimum_variance.strategy import DiagonalMinimumVarianceStrategy
from examples.minimum_variance.strategy import prepare_signals as prepare_minimum_variance
from examples.multi_leg_spread.strategy import MultiLegSpreadStrategy
from examples.multi_leg_spread.strategy import prepare_signals as prepare_spread
from examples.target_weights.strategy import TargetWeightsStrategy
from examples.topk_selection.strategy import TopKSelectionStrategy
from librae import (
    AccountSnapshot,
    Context,
    MultiLegOrder,
    PortfolioTargets,
    Position,
)

TS = datetime(2024, 1, 31, tzinfo=UTC)


def _close_panel(closes: dict[str, list[float]]) -> pd.DataFrame:
    timestamps = pd.date_range(
        "2024-01-01",
        periods=len(next(iter(closes.values()))),
        tz="UTC",
    )
    return pd.concat(
        {
            symbol: pd.DataFrame({"close": values}, index=timestamps)
            for symbol, values in closes.items()
        },
        names=["symbol", "datetime"],
    )


def _context(
    bars: dict[str, dict[str, float]],
    period_index: int = 20,
    positions: dict[str, Position] | None = None,
    symbols: tuple[str, ...] | None = None,
) -> Context:
    configured_symbols = symbols or tuple(bars)
    return Context(
        ts=TS,
        symbol=configured_symbols[0],
        symbols=configured_symbols,
        bar=bars.get(configured_symbols[0], {}),
        bars=bars,
        positions=positions or {},
        accounts={
            "default": AccountSnapshot(
                currency="USD",
                cash=100_000.0,
                equity=100_000.0,
            )
        },
        account_id_by_symbol={symbol: "default" for symbol in configured_symbols},
        period_index=period_index,
    )


def test_target_weights_strategy_submits_only_scheduled_non_null_weights() -> None:
    schedule = pd.DataFrame(
        [{"ALPHA": 0.6, "BETA": 0.35, "GAMMA": float("nan")}],
        index=[TS],
    )

    intent = TargetWeightsStrategy(schedule).on_bar(
        _context({"ALPHA": {}, "BETA": {}, "GAMMA": {}})
    )

    assert isinstance(intent, PortfolioTargets)
    assert intent.weights == {"ALPHA": 0.6, "BETA": 0.35}


def test_topk_strategy_selects_and_equal_weights_highest_scores() -> None:
    intent = TopKSelectionStrategy(top_k=2).on_bar(
        _context(
            {
                "ALPHA": {"score": 0.1},
                "BETA": {"score": 0.3},
                "GAMMA": {"score": 0.2},
            }
        )
    )

    assert isinstance(intent, PortfolioTargets)
    assert intent.weights == {"BETA": 0.475, "GAMMA": 0.475}


def test_minimum_variance_strategy_owns_risk_model_and_waits_for_complete_basket() -> None:
    strategy = DiagonalMinimumVarianceStrategy(target_exposure=0.90)

    incomplete = strategy.on_bar(
        _context(
            {
                "LOW_VOL": {"return_variance": 0.01},
                "HIGH_VOL": {"return_variance": 0.09},
            },
            symbols=("LOW_VOL", "MID_VOL", "HIGH_VOL"),
        )
    )
    optimized = strategy.on_bar(
        _context(
            {
                "LOW_VOL": {"return_variance": 0.01},
                "MID_VOL": {"return_variance": 0.04},
                "HIGH_VOL": {"return_variance": 0.09},
            }
        )
    )

    assert incomplete == []
    assert isinstance(optimized, PortfolioTargets)
    assert sum(optimized.weights.values()) == pytest.approx(0.90)
    assert (
        optimized.weights["LOW_VOL"] > optimized.weights["MID_VOL"] > optimized.weights["HIGH_VOL"]
    )


def test_minimum_variance_features_do_not_use_future_prices() -> None:
    panel = _close_panel(
        {
            "A": [100.0, 101.0, 103.0, 102.0, 104.0, 105.0],
            "B": [100.0, 99.0, 101.0, 100.0, 102.0, 101.0],
        }
    )
    changed_future = panel.copy()
    final_timestamp = panel.index.get_level_values("datetime").max()
    changed_future.loc[(slice(None), final_timestamp), "close"] *= 10
    prior_timestamp = panel.index.get_level_values("datetime").unique()[-2]

    original = prepare_minimum_variance(panel, lookback=3)
    changed = prepare_minimum_variance(changed_future, lookback=3)

    for symbol in ("A", "B"):
        assert original.loc[(symbol, prior_timestamp), "return_variance"] == pytest.approx(
            changed.loc[(symbol, prior_timestamp), "return_variance"]
        )


def test_multi_leg_spread_strategy_emits_sized_entry_and_exit_groups() -> None:
    strategy = MultiLegSpreadStrategy(
        "NEAR",
        "FAR",
        quantity=3.0,
        hedge_ratio=1.5,
    )
    entry = strategy.on_bar(
        _context(
            {
                "NEAR": {"spread_zscore": 2.0},
                "FAR": {"spread_zscore": 2.0},
            }
        )
    )

    positions = {
        "NEAR": Position("NEAR", "short", 101.0, 3.0, TS, 2, 0.0),
        "FAR": Position("FAR", "long", 99.0, 3.0, TS, 2, 0.0),
    }
    exit_group = strategy.on_bar(
        _context(
            {
                "NEAR": {"spread_zscore": 0.1},
                "FAR": {"spread_zscore": 0.1},
            },
            positions=positions,
        )
    )

    assert isinstance(entry, MultiLegOrder)
    assert [(leg.action, leg.symbol, leg.quantity) for leg in entry.legs] == [
        ("short", "NEAR", 3.0),
        ("long", "FAR", 4.5),
    ]
    assert isinstance(exit_group, MultiLegOrder)
    assert [leg.action for leg in exit_group.legs] == ["close", "close"]


def test_spread_features_do_not_use_future_prices() -> None:
    panel = _close_panel(
        {
            "NEAR": [101.0, 102.0, 104.0, 103.0, 105.0, 106.0],
            "FAR": [100.0, 100.5, 101.0, 101.5, 102.0, 102.5],
        }
    )
    changed_future = panel.copy()
    final_timestamp = panel.index.get_level_values("datetime").max()
    changed_future.loc[("NEAR", final_timestamp), "close"] = 1_000.0
    prior_timestamp = panel.index.get_level_values("datetime").unique()[-2]

    original = prepare_spread(panel, "NEAR", "FAR", lookback=3)
    changed = prepare_spread(changed_future, "NEAR", "FAR", lookback=3)

    assert original.loc[("NEAR", prior_timestamp), "spread_zscore"] == pytest.approx(
        changed.loc[("NEAR", prior_timestamp), "spread_zscore"]
    )
