"""Shared OHLCV value validation for historical and runtime engine inputs."""

from __future__ import annotations

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def validate_ohlcv_values(data: pd.DataFrame, *, context: str = "data") -> None:
    """Fail fast on OHLCV values that cannot safely drive fills or accounting."""
    missing = sorted(set(OHLCV_COLUMNS) - set(data.columns))
    if missing:
        raise ValueError(f"{context} missing required OHLCV columns: {', '.join(missing)}")
    if data.empty:
        raise ValueError(f"{context} must contain at least one OHLCV bar")

    non_numeric = [
        column for column in OHLCV_COLUMNS if not pd.api.types.is_numeric_dtype(data[column])
    ]
    if non_numeric:
        raise ValueError(f"{context} OHLCV columns must be numeric: {', '.join(non_numeric)}")

    values = data.loc[:, list(OHLCV_COLUMNS)].to_numpy(dtype=np.float64, na_value=np.nan)
    if not np.isfinite(values).all():
        raise ValueError(f"{context} OHLCV values must be finite")

    open_price, high, low, close, volume = values.T
    if np.any(open_price <= 0) or np.any(high <= 0) or np.any(low <= 0) or np.any(close <= 0):
        raise ValueError(f"{context} OHLC prices must be positive")
    if np.any(high < np.maximum.reduce([open_price, low, close])) or np.any(
        low > np.minimum.reduce([open_price, high, close])
    ):
        raise ValueError(
            f"{context} OHLC values are inconsistent: low <= open/close <= high is required"
        )
    if np.any(volume < 0):
        raise ValueError(f"{context} volume must be non-negative")
