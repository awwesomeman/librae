"""Offline conformance helpers for third-party Librae integrations."""

from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

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
