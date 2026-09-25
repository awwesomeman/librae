"""Offline conformance helpers for integrations and caller feature functions."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import numpy as np
import pandas as pd
from pandas.api.types import is_numeric_dtype

from librae.core.market_data import validate_ohlcv_values
from librae.live.executor import (
    REQUIRED_ORDER_ADAPTER_METHODS,
    ExecutionReport,
    LiveExecutor,
    OrderRequest,
)


def validate_order_adapter(adapter: object) -> None:
    """Raise when an object lacks a required live-order capability."""
    missing = [
        name
        for name in (*REQUIRED_ORDER_ADAPTER_METHODS, "execution_identity")
        if not callable(getattr(adapter, name, None))
    ]
    if missing:
        raise TypeError("order adapter is missing required methods: " + ", ".join(missing))


def normalize_broker_report(
    request: OrderRequest,
    report: Mapping[str, object],
    *,
    adapter: object | None = None,
) -> ExecutionReport:
    """Apply the same cumulative-report validation used by live execution.

    Pass the ``adapter`` that produced ``report`` when it declares a compact
    ``broker_client_order_id`` form so the client id check matches live.
    """
    return LiveExecutor.normalize_report(
        request,
        dict(report),
        broker_client_order_id=(
            LiveExecutor.broker_client_order_id(adapter, request) if adapter is not None else None
        ),
    )


def validate_bar_data(frame: pd.DataFrame) -> None:
    """Validate one polling adapter's canonical UTC bar frame."""
    if "ts" not in frame.columns:
        raise ValueError("bar data missing required ts column")
    timestamps = pd.to_datetime(frame["ts"], errors="raise")
    if timestamps.dt.tz is None:
        raise ValueError("bar data ts must be timezone-aware")
    if timestamps.duplicated().any():
        raise ValueError("bar data ts must be unique")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("bar data ts must be increasing")
    validate_ohlcv_values(frame, context="bar data")


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
    same = actual.eq(expected) | (actual.isna() & expected.isna())
    return ~same.fillna(False).to_numpy(dtype=bool)
