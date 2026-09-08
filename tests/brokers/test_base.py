"""Tests for shared broker-boundary helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from librae.brokers.base import drop_incomplete_ohlcv, validate_order_signal


def test_drop_incomplete_uses_calendar_session_close() -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp("2026-04-10 07:00Z")]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = datetime(2026, 4, 13, 4, 0, tzinfo=UTC)
        result = drop_incomplete_ohlcv(
            frame,
            "1d",
            calendar_id="XTAIFEX",
        )

    assert result.empty


@pytest.mark.parametrize(
    ("bar_start", "now", "expected_rows"),
    [
        ("2026-01-01 00:00Z", datetime(2026, 1, 31, 12, 0, tzinfo=UTC), 0),
        ("2026-02-01 00:00Z", datetime(2026, 3, 2, 0, 0, tzinfo=UTC), 1),
        ("2024-02-01 00:00Z", datetime(2024, 3, 1, 0, 0, tzinfo=UTC), 1),
        ("2025-12-01 00:00Z", datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 1),
    ],
)
def test_drop_incomplete_monthly_uses_real_utc_month_boundary(
    bar_start: str,
    now: datetime,
    expected_rows: int,
) -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp(bar_start)]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = now
        result = drop_incomplete_ohlcv(frame, "1M")

    assert len(result) == expected_rows


@pytest.mark.parametrize(
    ("now", "expected_rows"),
    [
        (datetime(2026, 1, 30, 20, 59, tzinfo=UTC), 0),
        (datetime(2026, 1, 30, 21, 1, tzinfo=UTC), 1),
    ],
)
def test_drop_incomplete_monthly_uses_last_exchange_session_close(
    now: datetime,
    expected_rows: int,
) -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp("2026-01-02 14:30Z")]})

    with patch("librae.brokers.base.datetime") as mocked_datetime:
        mocked_datetime.now.return_value = now
        result = drop_incomplete_ohlcv(frame, "MN1", calendar_id="XNYS")

    assert len(result) == expected_rows


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"side": "hold"}, "side"),
        ({"order_type": "stop"}, "order_type"),
        ({"time_in_force": "gtd"}, "time_in_force"),
        ({"quantity": 0}, "quantity"),
        ({"order_type": "limit"}, "limit price"),
    ],
)
def test_validate_order_signal_rejects_ambiguous_orders(overrides, match):
    signal = {
        "symbol": "TEST",
        "side": "buy",
        "quantity": 1.0,
        "order_type": "market",
        "time_in_force": "ioc",
    }
    signal.update(overrides)

    with pytest.raises(ValueError, match=match):
        validate_order_signal(signal)


def test_validate_order_signal_accepts_explicit_market_order():
    validate_order_signal(
        {
            "symbol": "TEST",
            "side": "sell",
            "quantity": 1.0,
            "order_type": "market",
            "time_in_force": "ioc",
        }
    )
