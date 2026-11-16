"""Offline conformance helpers for integrations and caller feature functions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from numbers import Real

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype


def validate_feature_causality(
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    bars: pd.DataFrame,
    *,
    cuts: int = 5,
    rtol: float = 1e-9,
    atol: float = 1e-12,
) -> None:
    """Raise when a caller feature function's rows depend on later bars.

    ``feature_fn`` is rerun on ``bars`` cut at ``cuts`` timestamps spread from
    the second to the second-to-last; each prefix's output must match the same
    rows of the full-history output. This catches look-ahead such as
    ``shift(-n)``, ``bfill``, centered windows, and full-sample statistics.
    ``bars`` is a ``DatetimeIndex`` frame or a ``(symbol, datetime)`` panel cut
    at the same instant for every symbol. Passing is evidence on this sample,
    not proof.
    """
    if cuts < 1:
        raise ValueError("cuts must be positive")
    timestamps = _row_timestamps(bars, "bars")
    if not bars.index.is_unique:
        raise ValueError("bars index must be unique")
    distinct = timestamps.unique().sort_values()
    if len(distinct) < cuts + 2:
        raise ValueError(f"bars need at least {cuts + 2} timestamps for {cuts} cuts")

    full = _features(feature_fn, bars)
    full_timestamps = _row_timestamps(full, "feature_fn output")
    positions = np.unique(np.linspace(len(distinct) - 2, 1, cuts).round().astype(int))
    for cut in distinct[positions]:
        prefix = _features(feature_fn, bars[timestamps <= cut])
        _require_same_rows(prefix, full[full_timestamps <= cut], cut, rtol=rtol, atol=atol)


def _row_timestamps(frame: pd.DataFrame, label: str) -> pd.DatetimeIndex:
    index = frame.index
    if isinstance(index, pd.MultiIndex) and "datetime" in index.names:
        index = index.get_level_values("datetime")
    if not isinstance(index, pd.DatetimeIndex):
        raise ValueError(f"{label} must use a DatetimeIndex or a (symbol, datetime) MultiIndex")
    return index


def _features(
    feature_fn: Callable[[pd.DataFrame], pd.DataFrame],
    bars: pd.DataFrame,
) -> pd.DataFrame:
    output = feature_fn(bars.copy())
    if not isinstance(output, pd.DataFrame):
        raise TypeError("feature_fn must return a pandas DataFrame")
    if duplicated := output.columns[output.columns.duplicated()].unique().tolist():
        raise ValueError(f"feature_fn output columns must be unique; duplicated: {duplicated}")
    return output


def _require_same_rows(
    prefix: pd.DataFrame,
    expected: pd.DataFrame,
    cut: pd.Timestamp,
    *,
    rtol: float,
    atol: float,
) -> None:
    extra_columns = sorted(set(prefix.columns) ^ set(expected.columns), key=str)
    if extra_columns:
        raise ValueError(f"feature_fn columns {extra_columns} depend on bars after cut at {cut}")
    if len(prefix.index.symmetric_difference(expected.index)):
        raise ValueError(f"feature_fn rows depend on bars after cut at {cut}")

    expected = expected.reindex(prefix.index)
    differs = pd.DataFrame(
        {
            column: _differs(prefix[column], expected[column], rtol=rtol, atol=atol)
            for column in prefix.columns
        },
        index=prefix.index,
    )
    rows = differs.any(axis=1).to_numpy()
    if rows.any():
        columns = [column for column in prefix.columns if differs[column].any()]
        timestamps = _row_timestamps(prefix, "feature_fn output")[rows]
        first = prefix.index[rows][timestamps.argmin()]
        raise ValueError(
            f"feature_fn columns {columns} at {first} differ from the full-history "
            f"output when bars are cut at {cut}"
        )


def _differs(actual: pd.Series, expected: pd.Series, *, rtol: float, atol: float) -> np.ndarray:
    if is_numeric_dtype(actual) and is_numeric_dtype(expected):
        return ~np.isclose(
            actual.to_numpy(dtype=float, na_value=np.nan),
            expected.to_numpy(dtype=float, na_value=np.nan),
            rtol=rtol,
            atol=atol,
            equal_nan=True,
        )
    same = [
        _same_cell(left, right, rtol=rtol, atol=atol)
        for left, right in zip(actual.to_numpy(), expected.to_numpy(), strict=True)
    ]
    return ~np.array(same, dtype=bool)


def _same_cell(left: object, right: object, *, rtol: float, atol: float) -> bool:
    """Compare object cells as numeric columns are: numbers within tolerance,
    missing equal to missing, recursing into dicts such as ``signal_inputs``,
    whose warm-up NaN must not read as look-ahead."""
    left_missing, right_missing = _is_missing(left), _is_missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _same_cell(left[key], right[key], rtol=rtol, atol=atol) for key in left
        )
    if _is_number(left) and _is_number(right):
        return bool(np.isclose(float(left), float(right), rtol=rtol, atol=atol))
    try:
        return bool(left == right)
    except (TypeError, ValueError):
        return False


def _is_missing(value: object) -> bool:
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float | np.floating) and bool(np.isnan(value))


def _is_number(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool | np.bool_)
