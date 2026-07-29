"""Run the explicitly sized multi-leg spread example.

uv run python -m examples.multi_leg_spread.run --mode backtest --no-db
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from librae import Backtest, RunConfig
from orchestration.cli import run_dispatch

from .strategy import MultiLegSpreadStrategy, prepare_signals


def _make_panel(symbols: list[str], periods: int = 260) -> pd.DataFrame:
    """Create deterministic prices with a mean-reverting relative spread."""
    if len(symbols) != 2:
        raise ValueError("multi-leg spread example requires exactly two symbols")
    rng = np.random.default_rng(23)
    timestamps = pd.date_range("2024-01-01", periods=periods, freq="D", tz="UTC")
    common = 100 + np.cumsum(rng.normal(0.02, 0.25, periods))
    spread = 3.0 * np.sin(np.arange(periods) / 8.0) + rng.normal(0, 0.20, periods)
    closes = {
        symbols[0]: common + spread / 2,
        symbols[1]: common - spread / 2,
    }
    frames: dict[str, pd.DataFrame] = {}
    for symbol, close in closes.items():
        open_ = np.concatenate(([close[0]], close[:-1]))
        frames[symbol] = pd.DataFrame(
            {
                "open": open_,
                "high": np.maximum(open_, close) * 1.002,
                "low": np.minimum(open_, close) * 0.998,
                "close": close,
                "volume": rng.uniform(10_000, 20_000, periods),
            },
            index=timestamps,
        )
    return pd.concat(frames, names=["symbol", "datetime"])


def run_backtest(config: RunConfig) -> None:
    params = config.params or {}
    near_symbol, far_symbol = config.symbols
    data = prepare_signals(
        _make_panel(config.symbols),
        near_symbol,
        far_symbol,
        lookback=int(params.get("lookback", 20)),
        hedge_ratio=float(params.get("hedge_ratio", 1.0)),
    )
    strategy = MultiLegSpreadStrategy(
        near_symbol,
        far_symbol,
        quantity=float(params.get("quantity", 10.0)),
        hedge_ratio=float(params.get("hedge_ratio", 1.0)),
        entry_zscore=float(params.get("entry_zscore", 1.5)),
        exit_zscore=float(params.get("exit_zscore", 0.25)),
        max_completion_seconds=float(params.get("max_completion_seconds", 3.0)),
    )
    backtest = Backtest(data=data, strategy=strategy, config=config)
    backtest.run()
    print(backtest.build_output().metrics)


if __name__ == "__main__":
    run_dispatch("multi_leg_spread_example", __file__, run_backtest)
