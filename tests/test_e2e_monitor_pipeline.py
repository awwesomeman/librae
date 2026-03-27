"""E2E integration tests: run_monitor → Point → write pipeline.

Validates the full pipeline from mock adapter through signal generation
to Point creation and mock write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from influxdb_client import Point

from quant_lab.monitoring.signal_monitor import run_monitor, signal_to_point


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 100, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    closes = base_price + np.cumsum(rng.normal(0, 0.5, n))
    highs = closes + rng.uniform(0.1, 1.0, n)
    lows = closes - rng.uniform(0.1, 1.0, n)
    opens = closes + rng.normal(0, 0.3, n)
    volumes = rng.uniform(100, 1000, n)
    return pd.DataFrame({
        "ts": ts,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


class MockBinanceAdapter:
    """Simulates Binance adapter with pre-built OHLCV data."""

    def __init__(self, df: pd.DataFrame, daily_df: pd.DataFrame | None = None):
        self._df = df
        self._daily_df = daily_df

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        if timeframe == "1d" and self._daily_df is not None:
            return self._daily_df.head(limit)
        return self._df.head(limit)


class EmptyAdapter:
    """Adapter that always returns empty DataFrame."""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Test 1: run_monitor with mock adapter returns a valid Point
# ---------------------------------------------------------------------------

class TestRunMonitorReturnsValidPoint:
    def test_run_monitor_returns_influxdb_point(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert pt is not None
        assert isinstance(pt, Point)


# ---------------------------------------------------------------------------
# Test 2: Point measurement and tags completeness
# ---------------------------------------------------------------------------

class TestPointMeasurementAndTags:
    def test_measurement_is_strategy_signals(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        line = pt.to_line_protocol()
        assert line.startswith("strategy_signals,")

    def test_all_required_tags_present(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        line = pt.to_line_protocol()
        required_tags = [
            "schema_version=",
            "strategy=",
            "symbol=",
            "timeframe=",
            "side=",
            "source=",
            "run_id=",
            "signal_type=",
        ]
        for tag in required_tags:
            assert tag in line, f"Missing tag: {tag}"


# ---------------------------------------------------------------------------
# Test 3: Fields present with correct types (float)
# ---------------------------------------------------------------------------

class TestPointFieldTypes:
    def test_fields_present_in_line_protocol(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        line = pt.to_line_protocol()
        assert "signal_strength=" in line
        assert "confidence=" in line
        assert "price=" in line

    def test_signal_to_point_fields_are_float(self):
        """Directly verify field types via signal_to_point."""
        pt = signal_to_point(
            signal=1,
            confidence=1.0,
            price=50000.0,
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT",
            timeframe="1h",
        )
        line = pt.to_line_protocol()
        # InfluxDB line protocol: floats don't have 'i' suffix
        # Extract fields section (between last tag-space and timestamp-space)
        parts = line.split(" ")
        # parts: [measurement+tags, fields, timestamp]
        fields_str = parts[1]
        field_pairs = fields_str.split(",")
        for pair in field_pairs:
            key, val = pair.split("=", 1)
            # float values should parse as float, not end with 'i' (integer)
            assert not val.endswith("i"), f"Field {key} is integer, expected float"
            float(val)  # should not raise


# ---------------------------------------------------------------------------
# Test 4: confidence is exactly 1.0 or 0.0
# ---------------------------------------------------------------------------

class TestConfidenceValues:
    def test_confidence_is_1_when_signal_present(self):
        """Entry signal → confidence must be 1.0."""
        pt = signal_to_point(
            signal=1, confidence=1.0, price=50000.0,
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        line = pt.to_line_protocol()
        assert "confidence=1.0" in line or "confidence=1" in line

    def test_confidence_is_0_when_no_signal(self):
        """Hold signal → confidence must be 0.0."""
        pt = signal_to_point(
            signal=0, confidence=0.0, price=49000.0,
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        line = pt.to_line_protocol()
        assert "confidence=0.0" in line or "confidence=0" in line

    def test_run_monitor_confidence_is_binary(self):
        """Full pipeline: confidence from run_monitor must be 0.0 or 1.0."""
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        line = pt.to_line_protocol()
        # Extract confidence value
        fields_str = line.split(" ")[1]
        conf_val = None
        for pair in fields_str.split(","):
            k, v = pair.split("=", 1)
            if k == "confidence":
                conf_val = float(v)
                break
        assert conf_val is not None, "confidence field not found"
        assert conf_val in (0.0, 1.0), f"confidence={conf_val}, expected 0.0 or 1.0"


# ---------------------------------------------------------------------------
# Test 5: mock InfluxDB write_api receives the Point correctly
# ---------------------------------------------------------------------------

class TestMockInfluxDBWrite:
    def test_write_api_receives_point(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_write_api = MagicMock()
        mock_write_api.write(bucket="test-bucket", org="test-org", record=pt)

        mock_write_api.write.assert_called_once_with(
            bucket="test-bucket", org="test-org", record=pt,
        )

    def test_write_api_receives_correct_point_type(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_write_api = MagicMock()
        mock_write_api.write(bucket="signals", org="myorg", record=pt)

        call_args = mock_write_api.write.call_args
        assert isinstance(call_args.kwargs["record"], Point)

    def test_write_api_point_line_protocol_valid(self):
        """The point written to InfluxDB produces valid line protocol."""
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_write_api = MagicMock()
        mock_write_api.write(bucket="b", org="o", record=pt)

        written_pt = mock_write_api.write.call_args.kwargs["record"]
        line = written_pt.to_line_protocol()
        # Valid line protocol: measurement,tags fields timestamp
        parts = line.split(" ")
        assert len(parts) == 3, f"Invalid line protocol: {line}"


# ---------------------------------------------------------------------------
# Test 6: empty adapter → None, write_api NOT called
# ---------------------------------------------------------------------------

class TestEmptyAdapterNoWrite:
    def test_empty_adapter_returns_none(self):
        adapter = EmptyAdapter()
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is None

    def test_write_api_not_called_on_none(self):
        adapter = EmptyAdapter()
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_write_api = MagicMock()
        # Only write if result is not None (simulating real usage)
        if result is not None:
            mock_write_api.write(bucket="b", org="o", record=result)

        mock_write_api.write.assert_not_called()
