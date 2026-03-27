"""Tests for scripts/etl/cache_store and cache integration in core_data_sources."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from pipeline.features.cache_store import build_cache_key, load_cache, save_cache


# ---------------------------------------------------------------------------
# build_cache_key
# ---------------------------------------------------------------------------

class TestBuildCacheKey(unittest.TestCase):
    def test_deterministic(self):
        k1 = build_cache_key("ns", {"a": 1, "b": 2})
        k2 = build_cache_key("ns", {"b": 2, "a": 1})
        self.assertEqual(k1, k2)

    def test_different_namespace_different_key(self):
        k1 = build_cache_key("spot", {"sym": "BTCUSDT"})
        k2 = build_cache_key("futures", {"sym": "BTCUSDT"})
        self.assertNotEqual(k1, k2)

    def test_different_params_different_key(self):
        k1 = build_cache_key("ns", {"start_ms": 1000})
        k2 = build_cache_key("ns", {"start_ms": 2000})
        self.assertNotEqual(k1, k2)

    def test_key_is_hex_string_of_expected_length(self):
        k = build_cache_key("ns", {})
        self.assertRegex(k, r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# TTL hit / miss
# ---------------------------------------------------------------------------

class TestLoadCacheTTL(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_cache_miss_when_file_absent(self):
        result = load_cache(self._tmpdir, "nonexistent_key", ttl_seconds=300)
        self.assertIsNone(result)

    def test_cache_hit_within_ttl(self):
        key = build_cache_key("test", {"x": 1})
        payload = [[1, "2.0", "3.0", "0.5", "1.5", "10", 0, 0, 0, 0, 0, 0]]
        save_cache(self._tmpdir, key, payload)
        result = load_cache(self._tmpdir, key, ttl_seconds=300)
        self.assertEqual(result, payload)

    def test_cache_miss_when_expired(self):
        key = build_cache_key("test", {"x": 2})
        payload = [[999]]
        save_cache(self._tmpdir, key, payload)
        # Back-date mtime by 10 seconds past TTL.
        path = os.path.join(self._tmpdir, f"{key}.json")
        old_time = time.time() - 11
        os.utime(path, (old_time, old_time))
        result = load_cache(self._tmpdir, key, ttl_seconds=10)
        self.assertIsNone(result)

    def test_ttl_zero_always_expired(self):
        key = build_cache_key("test", {"x": 3})
        save_cache(self._tmpdir, key, [[1]])
        result = load_cache(self._tmpdir, key, ttl_seconds=0)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Atomic save-load roundtrip
# ---------------------------------------------------------------------------

class TestSaveLoadRoundtrip(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_roundtrip_simple(self):
        key = build_cache_key("rt", {"k": "v"})
        payload = [[1706745600000, "42000", "43000", "41000", "42500", "100",
                    1706749199999, "0", "0", "0", "0", "0"]]
        save_cache(self._tmpdir, key, payload)
        loaded = load_cache(self._tmpdir, key, ttl_seconds=300)
        self.assertEqual(loaded, payload)

    def test_roundtrip_empty_payload(self):
        key = build_cache_key("rt", {"k": "empty"})
        save_cache(self._tmpdir, key, [])
        loaded = load_cache(self._tmpdir, key, ttl_seconds=300)
        self.assertEqual(loaded, [])

    def test_atomic_write_creates_final_file_no_tmp_leftover(self):
        key = build_cache_key("rt", {"k": "atomic"})
        save_cache(self._tmpdir, key, [[1]])
        files = os.listdir(self._tmpdir)
        self.assertIn(f"{key}.json", files)
        tmp_files = [f for f in files if f.endswith(".tmp")]
        self.assertEqual(tmp_files, [], "No .tmp files should remain after save")

    def test_overwrite_replaces_previous(self):
        key = build_cache_key("rt", {"k": "ow"})
        save_cache(self._tmpdir, key, [[1]])
        save_cache(self._tmpdir, key, [[2]])
        loaded = load_cache(self._tmpdir, key, ttl_seconds=300)
        self.assertEqual(loaded, [[2]])

    def test_cache_dir_created_automatically(self):
        nested = os.path.join(self._tmpdir, "a", "b", "c")
        key = build_cache_key("rt", {"k": "dir"})
        save_cache(nested, key, [[7]])
        loaded = load_cache(nested, key, ttl_seconds=300)
        self.assertEqual(loaded, [[7]])


# ---------------------------------------------------------------------------
# Integration-like: fetch functions use cache, bypassing network
# ---------------------------------------------------------------------------

class TestFetchSpotCacheIntegration(unittest.TestCase):
    """fetch_binance_spot_klines returns cached DataFrame without any network call."""

    KLINE_ROW = [
        1706745600000, "42000.00", "43000.00", "41000.00", "42500.00", "10.5",
        1706749199999, "441750.0", "120", "5.0", "210000.0", "0",
    ]

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_spot_cache_hit_skips_network(self):
        from pipeline.features.cache_store import build_cache_key, save_cache
        from pipeline.features.core_data_sources import fetch_binance_spot_klines

        key = build_cache_key(
            "binance_spot",
            {"symbol": "BTCUSDT", "interval": "1h",
             "start_ms": 1706745600000, "end_ms": 1706749200000},
        )
        save_cache(self._tmpdir, key, [self.KLINE_ROW])

        with patch("pipeline.features.core_data_sources._binance_request") as mock_req:
            df = fetch_binance_spot_klines(
                "BTCUSDT", "1h", 1706745600000, 1706749200000,
                cache_dir=self._tmpdir, cache_ttl_seconds=300,
            )
        mock_req.assert_not_called()
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["close"].iloc[0]), 42500.0)

    def test_spot_cache_miss_calls_network_and_stores(self):
        from pipeline.features.cache_store import build_cache_key, load_cache
        from pipeline.features.core_data_sources import fetch_binance_spot_klines

        with patch("pipeline.features.core_data_sources._binance_request") as mock_req, \
             patch("pipeline.features.core_data_sources._pace"):
            mock_req.return_value = [self.KLINE_ROW]
            df = fetch_binance_spot_klines(
                "BTCUSDT", "1h", 1706745600000, 1706749200000,
                cache_dir=self._tmpdir, cache_ttl_seconds=300,
            )

        mock_req.assert_called_once()
        self.assertEqual(len(df), 1)
        # Verify the cache was populated.
        key = build_cache_key(
            "binance_spot",
            {"symbol": "BTCUSDT", "interval": "1h",
             "start_ms": 1706745600000, "end_ms": 1706749200000},
        )
        cached = load_cache(self._tmpdir, key, ttl_seconds=300)
        self.assertIsNotNone(cached)
        self.assertEqual(len(cached), 1)


class TestFetchFuturesCacheIntegration(unittest.TestCase):
    """fetch_binance_futures_klines mirrors spot cache behaviour."""

    KLINE_ROW = [
        1706745600000, "30000.00", "31000.00", "29000.00", "30500.00", "20.0",
        1706749199999, "610000.0", "200", "10.0", "305000.0", "0",
    ]

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_futures_cache_hit_skips_network(self):
        from pipeline.features.cache_store import build_cache_key, save_cache
        from pipeline.features.core_data_sources import fetch_binance_futures_klines

        key = build_cache_key(
            "binance_futures",
            {"symbol": "BTCUSDT", "interval": "1h",
             "start_ms": 1706745600000, "end_ms": 1706749200000},
        )
        save_cache(self._tmpdir, key, [self.KLINE_ROW])

        with patch("pipeline.features.core_data_sources._binance_request") as mock_req:
            df = fetch_binance_futures_klines(
                "BTCUSDT", "1h", 1706745600000, 1706749200000,
                cache_dir=self._tmpdir, cache_ttl_seconds=300,
            )
        mock_req.assert_not_called()
        self.assertEqual(len(df), 1)
        self.assertAlmostEqual(float(df["close"].iloc[0]), 30500.0)

    def test_futures_namespace_differs_from_spot_key(self):
        params = {"symbol": "BTCUSDT", "interval": "1h",
                  "start_ms": 1706745600000, "end_ms": 1706749200000}
        k_spot = build_cache_key("binance_spot", params)
        k_fut = build_cache_key("binance_futures", params)
        self.assertNotEqual(k_spot, k_fut)


if __name__ == "__main__":
    unittest.main()
