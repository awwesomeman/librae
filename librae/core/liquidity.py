"""Point-in-time liquidity reference calculations."""

from __future__ import annotations

import numpy as np
import pandas as pd


def calculate_lagged_adv(
    volume: pd.Series,
    lookback_sessions: int,
    *,
    session_labels: pd.Index | None = None,
) -> pd.Series:
    """Return average volume from exactly N completed preceding sessions.

    With no labels, every row is one session (the D1 case).  Intraday callers
    provide one label per bar; volumes are summed by session before the lagged
    rolling mean is mapped back to every bar in the current session.
    """
    numeric_volume = volume.astype("float64")
    values = numeric_volume.to_numpy()
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("volume history must be finite and non-negative")
    if session_labels is None:
        return (
            numeric_volume.shift(1)
            .rolling(
                window=lookback_sessions,
                min_periods=lookback_sessions,
            )
            .mean()
        )
    if len(session_labels) != len(numeric_volume):
        raise ValueError("session_labels length must match volume history")

    labels = pd.Series(session_labels, index=numeric_volume.index, name="session_label")
    if labels.isna().any():
        raise ValueError("session_labels must not contain missing values")
    session_volume = numeric_volume.groupby(labels, sort=False).sum()
    lagged_session_adv = (
        session_volume.shift(1)
        .rolling(
            window=lookback_sessions,
            min_periods=lookback_sessions,
        )
        .mean()
    )
    return labels.map(lagged_session_adv).astype("float64")
