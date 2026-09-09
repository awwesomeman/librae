"""Run the strategy-owned minimum-variance allocation example.

uv run python -m examples.minimum_variance.run --mode backtest --no-db
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae import Backtest, RunConfig
from librae.orchestration.cli import RunOptions, run_dispatch

from examples._synthetic import session_opens

from .strategy import DiagonalMinimumVarianceStrategy, prepare_signals


def _make_panel(
    symbols: list[str],
    calendar_id: str,
    periods: int = 260,
) -> pd.DataFrame:
    """Create deterministic assets with different volatility levels."""
    rng = np.random.default_rng(19)
    timestamps = session_opens(calendar_id, start="2024-01-01", periods=periods)
    frames: dict[str, pd.DataFrame] = {}

    for index, symbol in enumerate(symbols):
        volatility = 0.006 + index * 0.001
        returns = rng.normal(0.0002, volatility, periods)
        close = 100 * np.cumprod(1 + returns)
        open_ = np.concatenate(([close[0]], close[:-1]))
        frames[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) * 1.002,
                "low": np.minimum(open_, close) * 0.998,
                "close": close,
                "volume": rng.uniform(100_000, 200_000, periods),
            },
            index=timestamps,
        )

    return pd.concat(frames, names=["symbol", "datetime"])


def run_backtest(config: RunConfig, _options: RunOptions) -> None:
    params = config.params or {}
    if config.calendar_id is None:
        raise ValueError("minimum-variance example requires calendar_id")
    data = prepare_signals(
        _make_panel(config.symbols, config.calendar_id),
        lookback=int(params.get("lookback", 20)),
    )
    strategy = DiagonalMinimumVarianceStrategy(
        rebalance_every=int(params.get("rebalance_every", 20)),
        target_exposure=float(params.get("target_exposure", 0.70)),
    )
    backtest = Backtest(
        data=data,
        strategy=strategy,
        config=config,
        record_position_snapshots=True,
    )
    backtest.run()
    print(backtest.build_output().metrics)


if __name__ == "__main__":
    run_dispatch("minimum_variance_example", __file__, run_backtest)
