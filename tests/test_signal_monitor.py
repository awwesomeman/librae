"""Tests for quant_lab.monitoring.signal_monitor.

Covers: run_monitor, SignalResult, adapter protocol, edge cases.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from quant_lab.monitoring.signal_monitor import (
    OHLCVAdapter,
    SignalResult,
    run_monitor,
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

class TestSignalResult:
    """Unit tests for SignalResult dataclass."""

    def test_entry_signal_type(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is not None
        assert result.signal_type in ("entry", "exit", "hold")

    def test_signal_type_entry_when_positive(self):
        r = SignalResult(
            ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=1, signal_type="entry", confidence=1.0, price=50000.0,
            signal_strength=1.0, run_id="test", source="live",
        )
        assert r.signal_type == "entry"
        assert r.signal == 1

    def test_signal_type_exit_when_negative(self):
        r = SignalResult(
            ts=datetime(2025, 3, 2, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=-1, signal_type="exit", confidence=1.0, price=49000.0,
            signal_strength=-1.0, run_id="test", source="live",
        )
        assert r.signal_type == "exit"
        assert r.signal == -1

    def test_signal_type_hold_when_zero(self):
        r = SignalResult(
            ts=datetime(2025, 3, 3, tzinfo=timezone.utc),
            symbol="BTC/USDT", strategy="TrendPullback", timeframe="1h",
            signal=0, signal_type="hold", confidence=0.0, price=49500.0,
            signal_strength=0.0, run_id="test", source="live",
        )
        assert r.signal_type == "hold"
        assert r.signal == 0

    def test_fields_present(self):
        r = SignalResult(
            ts=datetime(2025, 3, 1, tzinfo=timezone.utc),
            symbol="ETH/USDT", strategy="TrendPullback", timeframe="4h",
            signal=1, signal_type="entry", confidence=0.95, price=123.45,
            signal_strength=1.0, run_id="test", source="live",
        )
        assert r.signal_strength == 1.0
        assert r.confidence == 0.95
        assert r.price == 123.45


class TestRunMonitor:
    """Integration tests for run_monitor pipeline."""

    def test_returns_signal_result_with_valid_adapter(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result is not None
        assert isinstance(result, SignalResult)

    def test_strategy_is_trendpullback(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        result = run_monitor(adapter, symbol="BTC/USDT", timeframe="1h")
        assert result.strategy == "TrendPullback"

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
        result = run_monitor(adapter, symbol="ETH/USDT", timeframe="1h", params=params)
        assert result is not None

    def test_symbol_and_timeframe_in_result(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        result = run_monitor(adapter, symbol="ETH/USDT", timeframe="4h")
        assert result.symbol == "ETH/USDT"
        assert result.timeframe == "4h"

    def test_source_and_run_id(self):
        df = _make_ohlcv(100)
        adapter = MockAdapter(df)
        result = run_monitor(
            adapter, symbol="BTC/USDT", timeframe="1h",
            source="backfill", run_id="test-123",
        )
        assert result.source == "backfill"
        assert result.run_id == "test-123"


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
