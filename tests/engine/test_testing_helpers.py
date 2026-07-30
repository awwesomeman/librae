"""Tests for third-party integration conformance helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
from librae.live.executor import OrderRequest
from librae.testing import (
    normalize_broker_report,
    validate_bar_data,
    validate_order_adapter,
)


def _request() -> OrderRequest:
    return OrderRequest(
        client_order_id="client-1",
        symbol="BTCUSDT",
        venue_symbol="BTC/USDT",
        side="buy",
        quantity=1.0,
        order_type="market",
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_validate_order_adapter_accepts_complete_shape() -> None:
    validate_order_adapter(MagicMock())


def test_validate_order_adapter_lists_missing_methods() -> None:
    class IncompleteAdapter:
        def place_order(self, signal: dict) -> dict:
            return signal

    with pytest.raises(TypeError, match=r"prepare_order.*get_position"):
        validate_order_adapter(IncompleteAdapter())


def test_normalize_broker_report_uses_live_contract() -> None:
    report = normalize_broker_report(
        _request(),
        {
            "id": "order-1",
            "status": "filled",
            "amount": 1.0,
            "filled": 1.0,
            "average": 100.0,
            "commission": 0.1,
            "executed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
        },
    )

    assert report.order_id == "order-1"
    assert report.status == "filled"


def test_normalize_broker_report_rejects_invented_fill_facts() -> None:
    with pytest.raises(ValueError, match="commission"):
        normalize_broker_report(
            _request(),
            {
                "id": "order-1",
                "status": "filled",
                "amount": 1.0,
                "filled": 1.0,
                "average": 100.0,
                "executed_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
            },
        )


def test_validate_bar_data_accepts_canonical_frame() -> None:
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
        }
    )

    validate_bar_data(frame)


def test_validate_bar_data_rejects_naive_timestamps() -> None:
    frame = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=1, freq="h"),
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.5],
            "volume": [10.0],
        }
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        validate_bar_data(frame)
