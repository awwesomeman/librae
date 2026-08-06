"""Tests for shared broker-boundary helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from librae.brokers.base import drop_incomplete_ohlcv, validate_order_signal


def test_drop_incomplete_uses_calendar_session_close() -> None:
    frame = pd.DataFrame({"ts": [pd.Timestamp("2026-04-10 07:00Z")]})
    session_close = pd.Timestamp("2026-04-13 05:45Z")

    with (
        patch("librae.core.trading_calendar.bar_close", return_value=session_close),
        patch("librae.brokers.base.datetime") as mocked_datetime,
    ):
        mocked_datetime.now.return_value = datetime(2026, 4, 13, 4, 0, tzinfo=UTC)
        result = drop_incomplete_ohlcv(
            frame,
            "1d",
            calendar_id="XTAIFEX",
        )

    assert result.empty


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
