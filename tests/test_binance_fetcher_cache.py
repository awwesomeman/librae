"""Tests for binance_fetcher Parquet cache logic.

Uses mock httpx to verify cache hit/miss without real API calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quant_lab.data.binance_fetcher import (
    _cache_path,
    _is_cache_fresh,
    _CACHE_MAX_AGE,
    fetch_ohlcv,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_cache(tmp_path):
    """Return a temporary cache directory."""
    return tmp_path / "cache"


def _make_klines(n: int = 5, start_ms: int = 1_700_000_000_000, interval_ms: int = 3_600_000):
    """Build fake Binance klines response (list of lists)."""
    rows = []
    for i in range(n):
        ot = start_ms + i * interval_ms
        rows.append([
            ot,                  # open_time
            "100.0",             # open
            "105.0",             # high
            "95.0",              # low
            "102.0",             # close
            "1000.0",            # volume
            ot + interval_ms - 1,  # close_time
            "0", "0", "0", "0", "0",
        ])
    return rows


def _make_cached_df(n: int = 5, latest_ts: datetime | None = None) -> pd.DataFrame:
    """Build a small DataFrame that looks like cached output."""
    now = latest_ts or datetime.now(timezone.utc)
    ts = pd.date_range(end=now, periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "timestamp": ts,
        "open": [100.0] * n,
        "high": [105.0] * n,
        "low": [95.0] * n,
        "close": [102.0] * n,
        "volume": [1000.0] * n,
    })


# ---------------------------------------------------------------------------
# Tests: _cache_path
# ---------------------------------------------------------------------------

class TestCachePath:
    def test_returns_expected_filename(self, tmp_cache):
        p = _cache_path("BTCUSDT", "1h", cache_dir=tmp_cache)
        assert p.name == "BTCUSDT_1h.parquet"
        assert p.parent == tmp_cache

    def test_creates_directory(self, tmp_cache):
        _cache_path("ETHUSDT", "4h", cache_dir=tmp_cache)
        assert tmp_cache.exists()


# ---------------------------------------------------------------------------
# Tests: _is_cache_fresh
# ---------------------------------------------------------------------------

class TestIsCacheFresh:
    def test_missing_file_is_not_fresh(self, tmp_cache):
        p = tmp_cache / "nope.parquet"
        assert not _is_cache_fresh(p)

    def test_recent_cache_is_fresh(self, tmp_cache):
        now = datetime.now(timezone.utc)
        df = _make_cached_df(5, latest_ts=now - timedelta(hours=1))
        p = tmp_cache / "test.parquet"
        tmp_cache.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)
        assert _is_cache_fresh(p, now=now)

    def test_old_cache_is_not_fresh(self, tmp_cache):
        now = datetime.now(timezone.utc)
        df = _make_cached_df(5, latest_ts=now - timedelta(hours=12))
        p = tmp_cache / "test.parquet"
        tmp_cache.mkdir(parents=True, exist_ok=True)
        df.to_parquet(p, index=False)
        assert not _is_cache_fresh(p, now=now)

    def test_empty_cache_is_not_fresh(self, tmp_cache):
        p = tmp_cache / "empty.parquet"
        tmp_cache.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"]).to_parquet(p, index=False)
        assert not _is_cache_fresh(p)


# ---------------------------------------------------------------------------
# Tests: fetch_ohlcv cache integration
# ---------------------------------------------------------------------------

class TestFetchOhlcvCache:
    """Verify cache hit skips API; cache miss calls API and writes cache."""

    def test_cache_hit_skips_api(self, tmp_cache):
        """When cache is fresh, httpx should NOT be called."""
        now = datetime.now(timezone.utc)
        df = _make_cached_df(10, latest_ts=now - timedelta(hours=1))
        p = _cache_path("BTCUSDT", "1h", cache_dir=tmp_cache)
        df.to_parquet(p, index=False)

        with patch("quant_lab.data.binance_fetcher.httpx.Client") as mock_client:
            result = fetch_ohlcv("BTCUSDT", "1h", use_cache=True, cache_dir=tmp_cache)

        mock_client.assert_not_called()
        assert len(result) == 10

    def test_cache_miss_calls_api_and_writes(self, tmp_cache):
        """When no cache, API is called and result is written to parquet."""
        klines = _make_klines(3)

        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status = MagicMock()

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp

        with patch("quant_lab.data.binance_fetcher.httpx.Client", return_value=mock_http):
            result = fetch_ohlcv(
                "BTCUSDT", "1h",
                start="2023-11-14", end="2023-11-15",
                use_cache=True, cache_dir=tmp_cache,
            )

        assert len(result) == 3
        cache_file = _cache_path("BTCUSDT", "1h", cache_dir=tmp_cache)
        assert cache_file.exists()
        cached = pd.read_parquet(cache_file)
        assert len(cached) == 3

    def test_use_cache_false_always_calls_api(self, tmp_cache):
        """use_cache=False should always call API even if cache exists."""
        now = datetime.now(timezone.utc)
        df = _make_cached_df(5, latest_ts=now - timedelta(hours=1))
        p = _cache_path("BTCUSDT", "1h", cache_dir=tmp_cache)
        df.to_parquet(p, index=False)

        klines = _make_klines(2)
        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status = MagicMock()

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp

        with patch("quant_lab.data.binance_fetcher.httpx.Client", return_value=mock_http):
            result = fetch_ohlcv(
                "BTCUSDT", "1h",
                start="2023-11-14", end="2023-11-15",
                use_cache=False, cache_dir=tmp_cache,
            )

        mock_http.get.assert_called()
        assert len(result) == 2

    def test_cache_append_merges_new_bars(self, tmp_cache):
        """Second fetch should merge (not replace) with existing cache."""
        now = datetime.now(timezone.utc)
        old_df = _make_cached_df(3, latest_ts=now - timedelta(hours=10))
        p = _cache_path("BTCUSDT", "1h", cache_dir=tmp_cache)
        old_df.to_parquet(p, index=False)

        # New bars that don't overlap
        new_start_ms = int((now - timedelta(hours=2)).timestamp() * 1000)
        klines = _make_klines(2, start_ms=new_start_ms)

        mock_resp = MagicMock()
        mock_resp.json.return_value = klines
        mock_resp.raise_for_status = MagicMock()

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value=mock_http)
        mock_http.__exit__ = MagicMock(return_value=False)
        mock_http.get.return_value = mock_resp

        with patch("quant_lab.data.binance_fetcher.httpx.Client", return_value=mock_http):
            result = fetch_ohlcv(
                "BTCUSDT", "1h",
                start="2023-11-14", end="2023-11-15",
                use_cache=True, cache_dir=tmp_cache,
            )

        cached = pd.read_parquet(p)
        # Should have old (3) + new (2) = 5 rows (no overlap)
        assert len(cached) == 5
