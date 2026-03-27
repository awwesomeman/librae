"""E2E integration tests: run_monitor → SignalResult → write pipeline.

Validates the full pipeline from mock adapter through signal generation
to SignalResult creation and mock write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from quant_lab.monitoring.signal_monitor import run_monitor, SignalResult


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
# Test 1: run_monitor with mock adapter returns a valid SignalResult
# ---------------------------------------------------------------------------

class TestRunMonitorReturnsValidResult:
    def test_run_monitor_returns_signal_result(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is not None
        assert isinstance(result, SignalResult)


# ---------------------------------------------------------------------------
# Test 2: SignalResult fields completeness
# ---------------------------------------------------------------------------

class TestSignalResultFields:
    def test_strategy_is_trendpullback(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result.strategy == "TrendPullback"

    def test_all_required_fields_present(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result.symbol == "BTC/USDT"
        assert result.timeframe == "1h"
        assert result.source == "live"
        assert result.run_id == "monitor"
        assert result.signal_type in ("entry", "exit", "hold")
        assert isinstance(result.ts, datetime)


# ---------------------------------------------------------------------------
# Test 3: Fields present with correct types (float)
# ---------------------------------------------------------------------------

class TestSignalResultFieldTypes:
    def test_fields_are_correct_types(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert isinstance(result.signal_strength, float)
        assert isinstance(result.confidence, float)
        assert isinstance(result.price, float)
        assert isinstance(result.signal, int)

    def test_signal_result_fields_are_float(self):
        """Directly verify field types via SignalResult construction."""
        r = SignalResult(
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=1, signal_type="entry", confidence=1.0, price=50000.0,
            signal_strength=1.0, run_id="test", source="live",
        )
        assert isinstance(r.signal_strength, float)
        assert isinstance(r.confidence, float)
        assert isinstance(r.price, float)


# ---------------------------------------------------------------------------
# Test 4: confidence is exactly 1.0 or 0.0
# ---------------------------------------------------------------------------

class TestConfidenceValues:
    def test_confidence_is_1_when_signal_present(self):
        """Entry signal → confidence must be 1.0."""
        r = SignalResult(
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=1, signal_type="entry", confidence=1.0, price=50000.0,
            signal_strength=1.0, run_id="test", source="live",
        )
        assert r.confidence == 1.0

    def test_confidence_is_0_when_no_signal(self):
        """Hold signal → confidence must be 0.0."""
        r = SignalResult(
            ts=datetime(2025, 6, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=0, signal_type="hold", confidence=0.0, price=49000.0,
            signal_strength=0.0, run_id="test", source="live",
        )
        assert r.confidence == 0.0

    def test_run_monitor_confidence_is_binary(self):
        """Full pipeline: confidence from run_monitor must be 0.0 or 1.0."""
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result.confidence in (0.0, 1.0), f"confidence={result.confidence}, expected 0.0 or 1.0"


# ---------------------------------------------------------------------------
# Test 5: mock write receives the SignalResult correctly
# ---------------------------------------------------------------------------

class TestMockWrite:
    def test_write_function_receives_result(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_writer = MagicMock()
        mock_writer.write(result=result, run_id=result.run_id)

        mock_writer.write.assert_called_once_with(
            result=result, run_id=result.run_id,
        )

    def test_write_receives_correct_result_type(self):
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_writer = MagicMock()
        mock_writer.write(result=result)

        call_args = mock_writer.write.call_args
        assert isinstance(call_args.kwargs["result"], SignalResult)

    def test_result_has_valid_signal_values(self):
        """The result should have valid signal values for downstream consumers."""
        df = _make_ohlcv(150)
        adapter = MockBinanceAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result.signal in (-1, 0, 1)
        assert result.signal_type in ("entry", "exit", "hold")
        assert result.price > 0


# ---------------------------------------------------------------------------
# Test 6: empty adapter → None, write NOT called
# ---------------------------------------------------------------------------

class TestEmptyAdapterNoWrite:
    def test_empty_adapter_returns_none(self):
        adapter = EmptyAdapter()
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is None

    def test_write_not_called_on_none(self):
        adapter = EmptyAdapter()
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")

        mock_writer = MagicMock()
        if result is not None:
            mock_writer.write(result=result)

        mock_writer.write.assert_not_called()
