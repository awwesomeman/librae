"""Tests for quant_lab.monitoring.signal_monitor.

Covers: run_monitor, signal_to_point, adapter protocol, edge cases.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest
from influxdb_client import Point

from quant_lab.monitoring.signal_monitor import (
    OHLCVAdapter,
    run_monitor,
    signal_to_point,
    _prepare_dataframe,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 100, base_price: float = 100.0, seed: int = 42) -> pd.DataFrame:
    """Generate *n* rows of synthetic OHLCV with column ``ts``."""
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


class MockAdapter:
    """Mock adapter that returns pre-built OHLCV."""

    def __init__(self, df: pd.DataFrame, daily_df: pd.DataFrame | None = None):
        self._df = df
        self._daily_df = daily_df
        self.call_count = 0

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        self.call_count += 1
        if timeframe == "1d" and self._daily_df is not None:
            return self._daily_df.head(limit)
        return self._df.head(limit)


class EmptyAdapter:
    """Adapter that always returns empty DataFrame."""

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSignalToPoint:
    """Unit tests for signal_to_point formatting."""

    def test_entry_signal_produces_correct_measurement(self):
        pt = signal_to_point(
            signal=1, confidence=1.0, price=50000.0,
            ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", timeframe="1h",
        )
        line = pt.to_line_protocol()
        assert line.startswith("strategy_signals,")

    def test_entry_tags_aligned_with_influx_writer(self):
        pt = signal_to_point(
            signal=1, confidence=1.0, price=50000.0,
            ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", timeframe="1h",
        )
        line = pt.to_line_protocol()
        # All required tags present
        for tag in ("schema_version=", "strategy=", "symbol=", "timeframe=",
                     "side=", "source=", "run_id=", "signal_type="):
            assert tag in line, f"Missing tag: {tag}"

    def test_exit_signal_side_tag(self):
        pt = signal_to_point(
            signal=-1, confidence=1.0, price=49000.0,
            ts=datetime(2025, 3, 2, tzinfo=timezone.utc),
            symbol="BTC/USDT", timeframe="1h",
        )
        line = pt.to_line_protocol()
        assert "side=exit" in line
        assert "signal_type=exit" in line

    def test_hold_signal_side_tag(self):
        pt = signal_to_point(
            signal=0, confidence=0.0, price=49500.0,
            ts=datetime(2025, 3, 3, tzinfo=timezone.utc),
            symbol="BTC/USDT", timeframe="1h",
        )
        line = pt.to_line_protocol()
        assert "side=flat" in line
        assert "signal_type=hold" in line

    def test_fields_present(self):
        pt = signal_to_point(
            signal=1, confidence=0.95, price=123.45,
            ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
            symbol="ETH/USDT", timeframe="4h",
        )
        line = pt.to_line_protocol()
        assert "signal_strength=1.0" in line or "signal_strength=1" in line
        assert "confidence=0.95" in line
        assert "price=123.45" in line


class TestRunMonitor:
    """Integration tests for run_monitor pipeline."""

    def test_returns_point_with_valid_adapter(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert pt is not None
        assert isinstance(pt, Point)

    def test_measurement_is_strategy_signals(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        pt = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        line = pt.to_line_protocol()
        assert line.startswith("strategy_signals,")

    def test_returns_none_for_empty_adapter(self):
        adapter = EmptyAdapter()
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is None

    def test_adapter_called_with_limit(self):
        df = _make_ohlcv(200)
        adapter = MockAdapter(df)
        run_monitor(adapter, symbol="BTC/USDT", timeframe="1h", limit=150)
        # 2 calls: one for H1 data, one for D1 daily gate
        assert adapter.call_count == 2

    def test_custom_params_forwarded(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        params = {"ema_period": 10, "atr_period": 7}
        pt = run_monitor(adapter, symbol="ETH/USDT", timeframe="1h", params=params)
        assert pt is not None

    def test_symbol_and_timeframe_in_tags(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        pt = run_monitor(adapter, symbol="ETH/USDT", timeframe="4h")
        line = pt.to_line_protocol()
        assert "symbol=ETH/USDT" in line
        assert "timeframe=4h" in line

    def test_source_and_run_id_tags(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        pt = run_monitor(
            adapter, symbol="BTC/USDT", timeframe="1h",
            source="backfill", run_id="test-123",
        )
        line = pt.to_line_protocol()
        assert "source=backfill" in line
        assert "run_id=test-123" in line


class TestAdapterProtocol:
    """Protocol / duck-typing checks."""

    def test_mock_adapter_is_ohlcv_adapter(self):
        df = _make_ohlcv(10)
        adapter = MockAdapter(df)
        assert isinstance(adapter, OHLCVAdapter)

    def test_object_without_fetch_is_not_adapter(self):
        assert not isinstance(object(), OHLCVAdapter)


class TestPrepareDataframe:
    """Tests for the internal _prepare_dataframe helper."""

    def test_adds_feature_columns(self):
        df = _make_ohlcv(100)
        prepared = _prepare_dataframe(df)
        for col in ("ema20", "atr14", "vol_sma20", "daily_trend"):
            assert col in prepared.columns, f"Missing column: {col}"

    def test_index_is_datetime(self):
        df = _make_ohlcv(50)
        prepared = _prepare_dataframe(df)
        assert isinstance(prepared.index, pd.DatetimeIndex)

    def test_accepts_external_daily_df(self):
        """When daily_df is provided, _prepare_dataframe should use it."""
        df = _make_ohlcv(100)
        daily = _make_ohlcv(30, base_price=100.0, seed=99)
        # Resample to daily-like data
        daily["ts"] = pd.date_range("2025-01-01", periods=30, freq="1D", tz="UTC")
        prepared = _prepare_dataframe(df, daily_df=daily)
        assert "daily_trend" in prepared.columns
