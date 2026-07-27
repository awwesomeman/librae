"""Behavior tests for the runnable portfolio examples."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from examples.target_weights.strategy import TargetWeightsStrategy
from examples.topk_selection.strategy import TopKSelectionStrategy
from librae import Context, RebalanceTargets

TS = datetime(2024, 1, 31, tzinfo=UTC)


def _context(bars: dict[str, dict[str, float]], period_index: int = 20) -> Context:
    symbols = list(bars)
    return Context(
        ts=TS,
        symbol=symbols[0],
        symbols=symbols,
        bar=bars[symbols[0]],
        bars=bars,
        positions={},
        cash=100_000.0,
        equity=100_000.0,
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

    assert isinstance(intent, RebalanceTargets)
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

    assert isinstance(intent, RebalanceTargets)
    assert intent.weights == {"BETA": 0.475, "GAMMA": 0.475}
