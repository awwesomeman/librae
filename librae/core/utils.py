"""Core utility functions shared by backtest and live runtimes.

Provides:
- generate_run_id(): Deterministic-prefix run ID generation
- make_trade_id(): Canonical trade ID format
- infer_timeframe(): Infer canonical timeframe label from data index
- to_ccxt() / to_canonical(): Timeframe format conversion
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pandas as pd

# ---------------------------------------------------------------------------
# Run / Trade ID
# ---------------------------------------------------------------------------


def generate_run_id(strategy: str, symbol: str) -> str:
    """Deterministic-prefix run_id: <strategy>-<symbol>-<ts>-<short_uuid>."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    short = uuid.uuid4().hex[:8]
    return f"{strategy}-{symbol}-{ts}-{short}".lower().replace(" ", "_")


def make_trade_id(run_id: str, index: int) -> str:
    """Canonical trade ID: {run_id}-t{index:04d}."""
    return f"{run_id}-t{index:04d}"


# ---------------------------------------------------------------------------
# Timeframe utilities
# ---------------------------------------------------------------------------

# Canonical labels: M1, M5, M15, H1, H4, D1, W1
# ccxt format:     1m, 5m, 15m, 1h, 4h, 1d, 1w

_TIMEDELTA_TO_CANONICAL: dict[pd.Timedelta, str] = {
    pd.Timedelta(minutes=1): "M1",
    pd.Timedelta(minutes=5): "M5",
    pd.Timedelta(minutes=15): "M15",
    pd.Timedelta(hours=1): "H1",
    pd.Timedelta(hours=4): "H4",
    pd.Timedelta(days=1): "D1",
    pd.Timedelta(weeks=1): "W1",
}

_CANONICAL_TO_CCXT: dict[str, str] = {
    "M1": "1m", "M5": "5m", "M15": "15m",
    "H1": "1h", "H4": "4h", "D1": "1d", "W1": "1w",
}

_CCXT_TO_CANONICAL: dict[str, str] = {v: k for k, v in _CANONICAL_TO_CCXT.items()}


_MIN_BARS_FOR_INFERENCE = 5


def infer_timeframe(index: pd.DatetimeIndex) -> str:
    """Infer canonical timeframe label from data index.

    Uses timedelta mode（眾數）— the most frequent bar interval.
    Raises ValueError if bar count < 5 or mode is not mappable.
    """
    if len(index) < _MIN_BARS_FOR_INFERENCE:
        raise ValueError(
            f"Cannot infer timeframe from {len(index)} bars (minimum {_MIN_BARS_FOR_INFERENCE} required)"
        )
    diffs = pd.Series(index).diff().dropna()
    mode_td = diffs.mode().iloc[0]

    label = _TIMEDELTA_TO_CANONICAL.get(mode_td)
    if label is None:
        raise ValueError(
            f"Cannot map timedelta mode {mode_td} to a canonical timeframe. "
            f"Supported: {list(_TIMEDELTA_TO_CANONICAL.values())}"
        )
    return label


def to_ccxt(timeframe: str) -> str:
    """Convert canonical label to ccxt format. H1 → 1h."""
    canonical = timeframe.upper() if len(timeframe) == 2 else to_canonical(timeframe)
    result = _CANONICAL_TO_CCXT.get(canonical)
    if result is None:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Supported: {list(_CANONICAL_TO_CCXT.keys())}")
    return result


def to_canonical(timeframe: str) -> str:
    """Convert any format to canonical label. 1h → H1, H1 → H1."""
    upper = timeframe.upper()
    if upper in _CANONICAL_TO_CCXT:
        return upper
    lower = timeframe.lower()
    result = _CCXT_TO_CANONICAL.get(lower)
    if result is None:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Supported: {list(_CCXT_TO_CANONICAL.keys())}")
    return result
