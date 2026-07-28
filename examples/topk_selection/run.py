"""Run the cross-sectional Top-K selection example.

uv run python -m examples.topk_selection.run --mode backtest --no-db
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae import Backtest, RunConfig
from orchestration.cli import run_dispatch

from .strategy import TopKSelectionStrategy, prepare_signals


def _make_panel(symbols: list[str], periods: int = 240) -> pd.DataFrame:
    """Create deterministic data whose relative trends change halfway through."""
    rng = np.random.default_rng(11)
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    first_half_drifts = np.linspace(0.0010, -0.0004, len(symbols))
    frames: dict[str, pd.DataFrame] = {}

    for index, symbol in enumerate(symbols):
        drift = np.where(
            np.arange(periods) < periods // 2,
            first_half_drifts[index],
            first_half_drifts[::-1][index],
        )
        returns = drift + rng.normal(0, 0.006, periods)
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
    params = config.params or {}
    data = prepare_signals(
        _make_panel(config.symbols),
        lookback=int(params.get("lookback", 20)),
    )
    strategy = TopKSelectionStrategy(
        top_k=int(params.get("top_k", 2)),
        rebalance_every=int(params.get("rebalance_every", 20)),
        target_exposure=float(params.get("target_exposure", 0.95)),
    )
    backtest = Backtest(
        data=data,
        strategy=strategy,
        config=config,
        record_position_snapshots=True,
    )
    backtest.run()
    output = backtest.build_output()
    print(output.metrics)


def run_realtime(_config: RunConfig) -> None:
    raise NotImplementedError("PortfolioTargets examples currently support backtest mode only")


if __name__ == "__main__":
    run_dispatch("topk_selection_example", __file__, run_backtest, run_realtime)
