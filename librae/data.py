"""Explicit normalization for caller-owned backtest bar data."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

from librae.core.market_data import validate_ohlcv_values

_INDEX_NAMES = ["symbol", "datetime"]
_CANONICAL_FIELDS = frozenset((*_INDEX_NAMES, "open", "high", "low", "close", "volume"))


def normalize_bars(
    data: pd.DataFrame,
    *,
    symbol: str | None = None,
    column_mapping: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Return sorted UTC bars indexed by ``(symbol, datetime)``.

    ``data`` may use the canonical MultiIndex, canonical columns, or a
    DatetimeIndex for one explicitly named ``symbol``. ``column_mapping`` maps
    source column names to canonical names. Extra feature columns are preserved.
    """
    if not isinstance(data, pd.DataFrame):
        raise TypeError("data must be a pandas DataFrame")
    if symbol is not None and (not isinstance(symbol, str) or not symbol):
        raise ValueError("symbol must be a non-empty string")

    mapping = dict(column_mapping or {})
    unknown_targets = sorted(set(mapping.values()) - _CANONICAL_FIELDS)
    if unknown_targets:
        raise ValueError(
            "column_mapping targets must be canonical bar fields: " + ", ".join(unknown_targets)
        )
    missing_sources = sorted(set(mapping) - set(data.columns))
    if missing_sources:
        raise ValueError("column_mapping source columns not found: " + ", ".join(missing_sources))

    frame = data.rename(columns=mapping).copy()
    if not frame.columns.is_unique:
        raise ValueError("column_mapping creates duplicate columns")

    if isinstance(frame.index, pd.MultiIndex):
        if frame.index.nlevels != 2 or list(frame.index.names) != _INDEX_NAMES:
            raise ValueError("MultiIndex levels must be exactly ('symbol', 'datetime')")
        frame = frame.reset_index()
    elif isinstance(frame.index, pd.DatetimeIndex):
        if "datetime" in frame.columns:
            raise ValueError("datetime is present in both the index and columns")
        frame.insert(0, "datetime", frame.index)
        frame = frame.reset_index(drop=True)
    elif not isinstance(frame.index, pd.RangeIndex):
        raise ValueError("data must use a RangeIndex with a datetime column or a DatetimeIndex")

    if symbol is not None:
        if "symbol" in frame.columns:
            raise ValueError("pass either symbol or a symbol column, not both")
        frame.insert(0, "symbol", symbol)

    missing_index_fields = [name for name in _INDEX_NAMES if name not in frame.columns]
    if missing_index_fields:
        raise ValueError("data missing required index fields: " + ", ".join(missing_index_fields))

    symbols = frame["symbol"]
    if symbols.map(lambda value: not isinstance(value, str) or not value).any():
        raise ValueError("symbols must be non-empty strings")

    timestamps = pd.to_datetime(frame["datetime"], errors="raise")
    if not isinstance(timestamps.dtype, pd.DatetimeTZDtype):
        raise ValueError("datetime values must be timezone-aware")
    frame["datetime"] = timestamps.dt.tz_convert("UTC")

    if frame.duplicated(_INDEX_NAMES).any():
        raise ValueError("data must contain unique (symbol, datetime) pairs")

    normalized = frame.set_index(_INDEX_NAMES).sort_index()
    validate_ohlcv_values(normalized)
    return normalized
