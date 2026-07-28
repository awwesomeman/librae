"""Tests for librae.backtest.charts marker/frame building (pure functions only)."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from librae.backtest.charts import _build_markers, _df_to_order_events, _prepare_ohlcv
from librae.backtest.schema import OrderEventRecord


def _make_event(**kwargs) -> OrderEventRecord:
    defaults = dict(
        event_id="e1",
        ts=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
        account_id="default",
        currency="USDT",
        symbol="BTCUSDT",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=50_000.0,
        entry_price=50_000.0,
        remaining_quantity=1.0,
        notional=50_000.0,
    )
    defaults.update(kwargs)
    return OrderEventRecord(**defaults)


def test_build_markers_filters_by_symbol():
    events = [_make_event(symbol="BTCUSDT"), _make_event(symbol="ETHUSDT")]
    markers = _build_markers(events, "BTCUSDT")
    assert len(markers) == 1


def test_build_markers_long_entry_and_exit():
    entry = _make_event(side="long", event_type="open", price=50_000.0)
    exit_ = _make_event(
        side="long",
        event_type="close",
        price=51_000.0,
        net_return=2.0,
        ts=datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC),
    )
    markers = _build_markers([entry, exit_], "BTCUSDT")

    assert markers[0]["shape"] == "arrow_up"
    assert markers[0]["position"] == "below"
    assert "BUY" in markers[0]["text"]

    assert markers[1]["shape"] == "arrow_down"
    assert markers[1]["position"] == "above"
    assert "EXIT" in markers[1]["text"]
    assert "+2.0%" in markers[1]["text"]


def test_build_markers_short_entry_and_exit():
    entry = _make_event(side="short", event_type="open")
    exit_ = _make_event(
        side="short",
        event_type="close",
        ts=datetime(2026, 3, 2, 10, 0, 0, tzinfo=UTC),
    )
    markers = _build_markers([entry, exit_], "BTCUSDT")

    assert markers[0]["shape"] == "arrow_down"
    assert "SELL" in markers[0]["text"]
    assert markers[1]["shape"] == "arrow_up"


def test_build_markers_use_library_recognized_literals():
    """lightweight_charts.util.marker_position/marker_shape only recognize
    below/above/inside and arrow_up/arrow_down/circle/square — the JS-native
    spelling (belowBar/arrowUp) silently maps to None and drops the marker's
    position. Guards the exact regression that caused markers to render
    off-position."""
    lightweight_charts_util = pytest.importorskip("lightweight_charts.util")
    marker_position = lightweight_charts_util.marker_position
    marker_shape = lightweight_charts_util.marker_shape

    entry = _make_event(side="long", event_type="open")
    markers = _build_markers([entry], "BTCUSDT")
    assert marker_position(markers[0]["position"]) is not None
    assert marker_shape(markers[0]["shape"]) is not None


def test_build_markers_partial_close_no_duplicate_entry():
    """A position closed in two tranches must not produce two entry markers."""
    entry = _make_event(event_type="open")
    reduce_ = _make_event(
        event_type="reduce",
        ts=datetime(2026, 3, 2, tzinfo=UTC),
    )
    close_ = _make_event(
        event_type="close",
        ts=datetime(2026, 3, 3, tzinfo=UTC),
    )
    markers = _build_markers([entry, reduce_, close_], "BTCUSDT")
    entry_markers = [m for m in markers if "BUY" in m["text"]]
    exit_markers = [m for m in markers if "EXIT" in m["text"]]
    assert len(entry_markers) == 1
    assert len(exit_markers) == 2


def test_build_markers_sorted_by_time():
    later = _make_event(ts=datetime(2026, 3, 2, tzinfo=UTC))
    earlier = _make_event(ts=datetime(2026, 3, 1, tzinfo=UTC))
    markers = _build_markers([later, earlier], "BTCUSDT")
    assert markers[0]["time"] <= markers[1]["time"]


def test_prepare_ohlcv_builds_time_column():
    idx = pd.date_range("2026-03-01", periods=3, freq="4h")
    df = pd.DataFrame(
        {
            "open": [1, 2, 3],
            "high": [1, 2, 3],
            "low": [1, 2, 3],
            "close": [1, 2, 3],
            "volume": [10, 20, 30],
            "extra_col": [0, 0, 0],
        },
        index=idx,
    )
    out = _prepare_ohlcv(df)
    assert list(out.columns) == ["time", "open", "high", "low", "close", "volume"]
    assert out["time"].dtype == "datetime64[ns]"
    assert out["time"].iloc[0] == pd.Timestamp(idx[0])  # naive index: assumed already UTC


def test_prepare_ohlcv_without_volume():
    idx = pd.date_range("2026-03-01", periods=2, freq="1D")
    df = pd.DataFrame({"open": [1, 2], "high": [1, 2], "low": [1, 2], "close": [1, 2]}, index=idx)
    out = _prepare_ohlcv(df)
    assert "volume" not in out.columns


def test_prepare_ohlcv_matches_marker_epoch():
    """Candle epoch (ns-forced) and marker epoch (tz-aware .timestamp()) must land
    on the same second — this is the exact alignment that regressed under pandas 3."""
    idx = pd.date_range("2026-01-01", periods=3, freq="4h", tz="UTC")
    df = pd.DataFrame(
        {"open": [1, 2, 3], "high": [1, 2, 3], "low": [1, 2, 3], "close": [1, 2, 3]}, index=idx
    )
    out = _prepare_ohlcv(df)
    candle_epoch = out["time"].astype("int64") // 10**9

    from librae.backtest.charts import _to_utc

    marker_epoch = _to_utc(idx[1]).timestamp()
    assert int(marker_epoch) == candle_epoch.iloc[1]


def test_df_to_order_events_matches_load_trade_events_shape():
    """Mirrors db.timescale_reader.load_trade_events()'s column set (post-SQL, _time not yet renamed)."""
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
