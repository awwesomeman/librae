"""Tests for shared broker-boundary helpers."""

from __future__ import annotations

import pytest

from brokers.base import validate_order_signal


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"side": "hold"}, "side"),
        ({"order_type": "stop"}, "order_type"),
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
        }
    )
