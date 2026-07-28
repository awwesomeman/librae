"""Runnable example: the "run.py" a strategy repo is expected to write —
librae itself doesn't ship one (see architecture.md). Wires SmaCrossover
through librae's backtest and sim/live modes via orchestration/cli.py.

    uv run python -m examples.simple_sma.run --mode backtest
    uv run python -m examples.simple_sma.run --mode sim --poll-seconds 5

See examples/README.md for what's librae's job vs yours in this file.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae import Backtest, RunConfig
from orchestration.cli import run_dispatch, run_realtime_generic

from .strategy import SmaCrossover, prepare_signals


def _fetch_ohlcv(n: int = 500) -> pd.DataFrame:
    """Stand-in for a real fetch (exchange API / DB read) — swap this out.
    librae only cares about the output shape: columns
    [open, high, low, close, volume], indexed by a UTC datetime."""
    rng = np.random.default_rng(0)
    ts = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": rng.uniform(100, 1000, n),
        },
        index=ts,
    )


def run_backtest(config: RunConfig) -> None:
    df = prepare_signals(_fetch_ohlcv())
    # Backtest requires a MultiIndex(symbol, datetime) — see examples/README.md.
    df.index = pd.MultiIndex.from_arrays(
        [[config.symbol] * len(df), df.index], names=["symbol", "datetime"]
    )
    bt = Backtest(data=df, strategy=SmaCrossover(), config=config)
    bt.run()
    output = bt.build_output()
    print(
        output.metrics
    )  # swap for db.timescale_writer.save_strategy_results(output, df, config), a file, etc.


def run_realtime(config: RunConfig) -> None:
    run_realtime_generic(config, SmaCrossover(), prepare_signals)


if __name__ == "__main__":
    run_dispatch("sma_crossover_example", __file__, run_backtest, run_realtime)
