"""Tests for the TimescaleDB trade-chart adapter."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from librae.backtest.charts import _build_markers
from librae.db.charts import _df_to_order_events


def test_df_to_order_events_matches_reader_shape() -> None:
    df = pd.DataFrame(
        [
            {
                "event_id": "e1",
                "_time": datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
                "account_id": "default",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "long",
                "event_type": "open",
                "fill_quantity": 1.0,
                "price": 50_000.0,
                "entry_price": 50_000.0,
                "remaining_quantity": 1.0,
                "notional": 50_000.0,
                "commission": 1.0,
                "slippage": 0.0,
                "tax": 0.0,
                "pnl": None,
                "net_return": None,
                "entry_at": None,
                "periods_held": None,
                "reason": "",
            }
        ]
    )

    events = _df_to_order_events(df)

    assert len(events) == 1
    assert events[0].symbol == "BTCUSDT"
    assert events[0].ts == datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
    markers = _build_markers(events, "BTCUSDT")
    assert len(markers) == 1
    assert markers[0]["shape"] == "arrow_up"
