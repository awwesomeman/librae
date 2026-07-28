"""Point-in-time liquidity reference calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_lagged_adv(volume: pd.Series, lookback_sessions: int) -> pd.Series:
    """Return average volume from exactly N completed preceding D1 bars.

    The current bar is shifted out before rolling, so the value available at
    an execution bar never contains that bar's eventual volume.
    """
    numeric_volume = volume.astype("float64")
    values = numeric_volume.to_numpy()
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("volume history must be finite and non-negative")
    return (
        numeric_volume.shift(1)
        .rolling(
            window=lookback_sessions,
            min_periods=lookback_sessions,
        )
        .mean()
    )
