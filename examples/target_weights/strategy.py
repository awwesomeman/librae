"""Execute a precomputed target-weight schedule."""

from __future__ import annotations

import pandas as pd
from librae import Context, PortfolioTargets, Strategy, StrategyDecision


class TargetWeightsStrategy(Strategy):
    """Submit weights prepared by an external allocator on scheduled dates."""

    def __init__(self, target_weights: pd.DataFrame) -> None:
        self._target_weights = target_weights

    def on_bar(self, ctx: Context) -> StrategyDecision:
        if ctx.ts not in self._target_weights.index:
            return []

        row = self._target_weights.loc[ctx.ts]
        weights = {str(symbol): float(weight) for symbol, weight in row.items() if pd.notna(weight)}
        return PortfolioTargets(
            weights=weights,
            reason="scheduled_allocation",
        )
