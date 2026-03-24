"""Generate deterministic synthetic OHLCV data for PoC comparison.

Produces 1-minute BTC-like data. Resampling uses scripts.etl.core_features.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def generate_btc_1m(
    start: str = "2025-01-01",
    end: str = "2025-03-31",
    seed: int = 42,
    initial_price: float = 95000.0,
) -> pd.DataFrame:
    """Generate synthetic 1-minute OHLCV data resembling BTC."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq="min", tz="UTC")
    n = len(idx)

    log_returns = rng.normal(0, 0.0003, n)
    log_prices = np.log(initial_price) + np.cumsum(log_returns)
    close = np.exp(log_prices)

    noise_h = np.abs(rng.normal(0, 0.0002, n))
    noise_l = np.abs(rng.normal(0, 0.0002, n))
    high = close * (1 + noise_h)
    low = close * (1 - noise_l)
    open_ = np.roll(close, 1) * (1 + rng.normal(0, 0.00005, n))
    open_[0] = initial_price
    high = np.maximum(high, np.maximum(open_, close))
    low = np.minimum(low, np.minimum(open_, close))

    volume = rng.lognormal(mean=8, sigma=1.5, size=n)

    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    df.index.name = "ts"
    return df
