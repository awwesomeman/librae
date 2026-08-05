"""Allocate a complete universe with a strategy-owned diagonal risk model."""

from __future__ import annotations

from math import isfinite

import pandas as pd
from librae import Context, PortfolioWeights, Strategy, StrategyDecision


def prepare_signals(df: pd.DataFrame, lookback: int = 20) -> pd.DataFrame:
    """Estimate trailing return variance independently for each symbol."""
    if lookback < 2:
        raise ValueError("lookback must be at least 2")
    out = df.copy()
    returns = out.groupby(level="symbol", sort=False)["close"].pct_change()
    out["return_variance"] = returns.groupby(
        level="symbol",
        sort=False,
        group_keys=False,
    ).transform(
        lambda series: series.rolling(
            lookback,
            min_periods=lookback,
        ).var(ddof=1)
    )
    return out


class DiagonalMinimumVarianceStrategy(Strategy):
    """Solve long-only minimum variance under a diagonal covariance model.

    The risk estimate, objective, and rebalance schedule deliberately live in
    strategy code. Replacing the diagonal model with a full covariance model
    or a constrained solver does not change Librae's decision API.
    """

    def __init__(
        self,
        rebalance_every: int = 20,
        target_exposure: float = 0.70,
    ) -> None:
        if rebalance_every <= 0:
            raise ValueError("rebalance_every must be positive")
        if not 0 < target_exposure <= 1:
            raise ValueError("target_exposure must be in (0, 1]")
        self._rebalance_every = rebalance_every
        self._target_exposure = target_exposure

    def on_bar(self, ctx: Context) -> StrategyDecision:
        if ctx.period_index % self._rebalance_every:
            return []
        if set(ctx.available_symbols) != set(ctx.symbols):
            return []

        inverse_variances: dict[str, float] = {}
        for symbol in ctx.symbols:
            variance = ctx.bars[symbol].get("return_variance")
            if variance is None or not isfinite(variance) or variance <= 0:
                return []
            inverse_variance = 1.0 / variance
            if not isfinite(inverse_variance):
                return []
            inverse_variances[symbol] = inverse_variance

        normalizer = sum(inverse_variances.values())
        if not isfinite(normalizer) or normalizer <= 0:
            return []
        weights = {
            symbol: self._target_exposure * inverse_variances[symbol] / normalizer
            for symbol in ctx.symbols
        }
        return PortfolioWeights(
            weights=weights,
            reason="diagonal_minimum_variance",
        )
