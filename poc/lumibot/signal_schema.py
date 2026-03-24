"""Shared signal schema for both reference and Lumibot strategies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import pandas as pd

SIGNAL_COLUMNS = ["timestamp", "side", "symbol", "strategy", "run_id"]


@dataclass(frozen=True)
class Signal:
    timestamp: pd.Timestamp
    side: str
    symbol: str
    strategy: str
    run_id: str


def signals_to_df(signals: Sequence[Signal]) -> pd.DataFrame:
    """Convert signal list to DataFrame."""
    if not signals:
        return pd.DataFrame(columns=SIGNAL_COLUMNS)
    return pd.DataFrame(
        [
            {
                "timestamp": s.timestamp,
                "side": s.side,
                "symbol": s.symbol,
                "strategy": s.strategy,
                "run_id": s.run_id,
            }
            for s in signals
        ]
    )
