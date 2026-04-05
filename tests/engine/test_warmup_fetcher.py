"""Tests for DB-first warmup fetcher in wiring.py."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


def _make_ohlcv_df(n: int) -> pd.DataFrame:
    ts = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": range(n),
        "high": [x + 1 for x in range(n)],
        "low": [max(0, x - 1) for x in range(n)],
        "close": [x + 0.5 for x in range(n)],
        "volume": [100.0] * n,
    })


class TestWarmupFetcher:
    """LiveTrader warmup_fetcher uses get_ohlcv for initial load."""

    def test_warmup_from_db_skips_exchange_api(self):
        """When warmup_fetcher returns data, regular fetcher is not called."""
        from librae.live.engine import LiveTrader

        warmup_df = pd.DataFrame({
            "ts": pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC"),
            "open": range(100), "high": range(100), "low": range(100),
            "close": range(100), "volume": [100] * 100,
        })
        mock_warmup = MagicMock(return_value=warmup_df)
        mock_fetcher = MagicMock()
        mock_strategy = MagicMock()
        mock_executor = MagicMock()

        trader = LiveTrader(
            strategy=mock_strategy,
            symbols=["BTCUSDT"],
            fetcher=mock_fetcher,
            feature_fn=lambda x: x,
            executor=mock_executor,
            warmup_fetcher=mock_warmup,
            warmup_periods=50,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        assert result is not None
        assert len(result) == 100
        mock_warmup.assert_called_once_with("BTCUSDT", trader._timeframe, 50)
        mock_fetcher.assert_not_called()

    def test_warmup_fetcher_none_uses_regular_fetcher(self):
        """When warmup_fetcher is None, uses regular fetcher for warmup."""
        from librae.live.engine import LiveTrader

        mock_strategy = MagicMock()
        mock_executor = MagicMock()
        warmup_df = pd.DataFrame({
            "ts": pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
            "open": range(10), "high": range(10), "low": range(10),
            "close": range(10), "volume": [100] * 10,
        })
        mock_fetcher = MagicMock(return_value=warmup_df)

        trader = LiveTrader(
            strategy=mock_strategy,
            symbols=["BTCUSDT"],
            fetcher=mock_fetcher,
            feature_fn=lambda x: x,
            executor=mock_executor,
            warmup_fetcher=None,
            warmup_periods=10,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        mock_fetcher.assert_called_once()
        assert len(result) == 10
