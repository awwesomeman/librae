"""Unit tests for Binance data fetcher.

Skills: python, quant

Tests use httpx mock transport to avoid real API calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pandas as pd
import pytest

from nautilus_lab.data.binance_fetcher import (
    BINANCE_KLINES_URL,
    fetch_ohlcv,
    _parse_dt,
    _subtract_months,
)


def _make_kline(open_time_ms: int, price: float = 50000.0, volume: float = 100.0) -> list:
    """Build a single Binance kline row."""
    return [
        open_time_ms,
        str(price),
        str(price * 1.01),
        str(price * 0.99),
        str(price * 1.005),
        str(volume),
        open_time_ms + 3599999,
        str(price * volume),
        42,
        str(volume * 0.6),
        str(price * volume * 0.6),
        "0",
    ]


class _MockTransport(httpx.BaseTransport):
    """Returns synthetic klines for testing pagination."""

    def __init__(self, total_bars: int = 5):
        self.total_bars = total_bars
        self._call_count = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self._call_count += 1
        start_ms = int(request.url.params.get("startTime", "0"))
        limit = int(request.url.params.get("limit", "1000"))

        klines = []
        cursor = start_ms
        for i in range(min(limit, self.total_bars)):
            klines.append(_make_kline(cursor, price=50000 + i * 10))
            cursor += 3600000
            self.total_bars -= 1
            if self.total_bars <= 0:
                break
        return httpx.Response(200, json=klines)


class TestParseHelpers:
    def test_parse_dt_string(self):
        dt = _parse_dt("2024-01-01")
        assert dt.tzinfo is not None
        assert dt.year == 2024

    def test_parse_dt_datetime(self):
        dt = _parse_dt(datetime(2024, 6, 15, tzinfo=timezone.utc))
        assert dt.month == 6

    def test_subtract_months(self):
        dt = datetime(2024, 6, 15, tzinfo=timezone.utc)
        result = _subtract_months(dt, 6)
        assert result.month == 12
        assert result.year == 2023


class TestFetchOHLCV:
    def test_returns_correct_columns(self):
        transport = _MockTransport(total_bars=3)
        original_init = httpx.Client.__init__

        def patched_init(self_client, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.pop("timeout", None)
            original_init(self_client, *args, timeout=30.0, **kwargs)

        import unittest.mock as mock
        with mock.patch.object(httpx.Client, "__init__", patched_init):
            df = fetch_ohlcv(
                symbol="BTCUSDT",
                interval="1h",
                start="2024-01-01",
                end="2024-01-02",
            )
        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert len(df) == 3
        assert df["timestamp"].dt.tz is not None

    def test_empty_response(self):
        transport = _MockTransport(total_bars=0)
        import unittest.mock as mock

        def patched_init(self_client, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs.pop("timeout", None)
            httpx.Client.__init__.__wrapped__(self_client, *args, timeout=30.0, **kwargs) if hasattr(httpx.Client.__init__, '__wrapped__') else None

        # Use a simpler approach
        with mock.patch("nautilus_lab.data.binance_fetcher.httpx.Client") as MockClient:
            mock_client = mock.MagicMock()
            mock_resp = mock.MagicMock()
            mock_resp.json.return_value = []
            mock_resp.raise_for_status.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client.__enter__ = mock.MagicMock(return_value=mock_client)
            mock_client.__exit__ = mock.MagicMock(return_value=False)
            MockClient.return_value = mock_client

            df = fetch_ohlcv(symbol="BTCUSDT", interval="1h", start="2024-01-01", end="2024-01-02")
        assert len(df) == 0
        assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]

    def test_dtypes_are_float(self):
        import unittest.mock as mock
        klines = [_make_kline(1704067200000 + i * 3600000) for i in range(5)]
        with mock.patch("nautilus_lab.data.binance_fetcher.httpx.Client") as MockClient:
            mock_client = mock.MagicMock()
            mock_resp = mock.MagicMock()
            mock_resp.json.return_value = klines
            mock_resp.raise_for_status.return_value = None
            mock_client.get.return_value = mock_resp
            mock_client.__enter__ = mock.MagicMock(return_value=mock_client)
            mock_client.__exit__ = mock.MagicMock(return_value=False)
            MockClient.return_value = mock_client

            df = fetch_ohlcv(symbol="BTCUSDT", interval="1h", start="2024-01-01", end="2024-01-02")
        for col in ("open", "high", "low", "close", "volume"):
            assert df[col].dtype == float, f"{col} should be float"
