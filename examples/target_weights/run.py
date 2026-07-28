"""Run the scheduled target-weight example.

uv run python -m examples.target_weights.run --mode backtest --no-db
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae import Backtest, RunConfig
from orchestration.cli import run_dispatch

from .strategy import TargetWeightsStrategy


def _make_panel(symbols: list[str], periods: int = 180) -> pd.DataFrame:
    """Create deterministic synthetic daily OHLCV data."""
    rng = np.random.default_rng(7)
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    frames: dict[str, pd.DataFrame] = {}

    for index, symbol in enumerate(symbols):
        returns = rng.normal(0.0002 + index * 0.0001, 0.01, periods)
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


def run_backtest(config: RunConfig) -> None:
    data = _make_panel(config.symbols)
    timestamps = data.index.get_level_values("datetime").unique()
    target_weights = pd.DataFrame(
        [
            {config.symbols[0]: 0.60, config.symbols[1]: 0.35},
            {config.symbols[1]: 0.45, config.symbols[2]: 0.50},
            {config.symbols[0]: 0.30, config.symbols[2]: 0.65},
        ],
        index=timestamps[[20, 80, 140]],
    )
    backtest = Backtest(
        data=data,
        strategy=TargetWeightsStrategy(target_weights),
        config=config,
        record_position_snapshots=True,
    )
    backtest.run()
    output = backtest.build_output()
    print(output.metrics)


def run_realtime(_config: RunConfig) -> None:
    raise NotImplementedError("PortfolioTargets examples currently support backtest mode only")


if __name__ == "__main__":
    run_dispatch("target_weights_example", __file__, run_backtest, run_realtime)
