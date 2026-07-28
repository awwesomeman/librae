"""Golden signal-outcome fixture shared by Python and Grafana contract tests."""

from __future__ import annotations

import pandas as pd

SIGNAL_OUTCOME_LONG_PERCENTAGE_POINTS = {
    "forward_return": [5.0, 2.0, 0.0],
    "mfe": [10.0, 10.0, 10.0],
    "mae": [5.0, 5.0, 5.0],
}
SIGNAL_OUTCOME_LONG_FRACTIONS = {
    name: [value / 100.0 for value in values]
    for name, values in SIGNAL_OUTCOME_LONG_PERCENTAGE_POINTS.items()
}


def make_signal_outcome_contract_ohlcv() -> pd.DataFrame:
    """Return one signal bar, one reference bar, and four forward bars."""
    index = pd.date_range("2026-03-01", periods=6, freq="1h", tz="UTC")
    return pd.DataFrame(
        {
            "open": [100, 100, 100, 100, 100, 100],
            "high": [100, 102, 110, 108, 100, 100],
            "low": [100, 99, 95, 97, 100, 100],
            "close": [100, 101, 105, 102, 100, 100],
        },
        index=index,
    )
