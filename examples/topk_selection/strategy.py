"""Rank a multi-asset universe and hold the highest-scoring symbols."""

from __future__ import annotations

from math import isfinite

import pandas as pd
from librae import Context, PortfolioTargets, Strategy, StrategyDecision


def prepare_signals(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """Add trailing-return scores independently within each symbol."""
    out = df.copy()
    out["score"] = out.groupby(level="symbol")["close"].pct_change(lookback)
    return out


class TopKSelectionStrategy(Strategy):
    """Select the Top K symbols by score and allocate equal target weights."""

    def __init__(
        self,
        top_k: int = 2,
        rebalance_every: int = 20,
        target_exposure: float = 0.95,
    ) -> None:
        if top_k <= 0:
            raise ValueError("top_k must be positive")
        if rebalance_every <= 0:
            raise ValueError("rebalance_every must be positive")
        if not 0 < target_exposure <= 1:
            raise ValueError("target_exposure must be in (0, 1]")
        self._top_k = top_k
        self._rebalance_every = rebalance_every
        self._target_exposure = target_exposure

    def on_bar(self, ctx: Context) -> StrategyDecision:
        if ctx.period_index % self._rebalance_every:
            return []

        scores: dict[str, float] = {}
        for symbol, bar in ctx.bars.items():
            score = bar.get("score")
            if score is not None and isfinite(score):
                scores[symbol] = score
        if not scores:
            return []

        selected = sorted(scores, key=lambda symbol: (-scores[symbol], symbol))[: self._top_k]
        weight = self._target_exposure / len(selected)
        if not set(ctx.positions).issubset(ctx.available_symbols):
            return []
        return PortfolioTargets(
            weights={symbol: weight for symbol in selected},
            reason="top_k_selection",
        )
