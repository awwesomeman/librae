import unittest
from unittest.mock import patch, MagicMock
import pandas as pd

from pipeline.features.core_features import resample_ohlcv, add_daily_trend_gate, add_multifactor_features
from pipeline.features.core_data_sources import (
    normalize_ohlcv,
    _binance_request,
    _chunk_ranges,
)


class TestEtlDecoupling(unittest.TestCase):
    def test_normalize_ohlcv(self):
        raw = pd.DataFrame({
            "ts": ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"],
            "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20],
        })
        out = normalize_ohlcv(raw)
        self.assertEqual(list(out.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(len(out), 2)

    def test_normalize_ohlcv_with_rename(self):
        raw = pd.DataFrame({
            "open_time": ["2026-01-01T00:00:00Z"],
            "open": [1], "high": [2], "low": [0.5], "close": [1.5], "volume": [10],
        })
        out = normalize_ohlcv(raw, ts_col="open_time")
        self.assertEqual(len(out), 1)
        self.assertTrue(out.index.name == "ts")

    def test_normalize_ohlcv_drops_na(self):
        raw = pd.DataFrame({
            "ts": ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"],
            "open": [1, None], "high": [2, 3], "low": [0.5, 1.5],
            "close": [1.5, 2.5], "volume": [10, 20],
        })
        out = normalize_ohlcv(raw)
        self.assertEqual(len(out), 1)

    def test_feature_pipeline(self):
        idx = pd.date_range("2026-01-01", periods=120, freq="min", tz="UTC")
        df = pd.DataFrame(index=idx, data={
            "open": range(120),
            "high": [x + 1 for x in range(120)],
            "low": [x - 1 for x in range(120)],
            "close": range(120),
            "volume": [100] * 120,
        })
        h1 = resample_ohlcv(df, "60min")
        feat = add_multifactor_features(h1)
        d1 = add_daily_trend_gate(resample_ohlcv(df, "1D"))
        self.assertIn("ema60", feat.columns)
        self.assertIn("atr14", feat.columns)
        self.assertIn("ema20_prev", d1.columns)


# -----------------------------------------------------------------------
# Backoff helper tests (offline, deterministic)
# -----------------------------------------------------------------------

class TestBinanceRequest(unittest.TestCase):
    """Validate _binance_request retry / backoff behaviour."""

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_success_on_first_try(self, mock_get, mock_sleep):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = [["data"]]
        mock_get.return_value = resp

        result = _binance_request("https://api.binance.com/api/v3/klines", {})
        self.assertEqual(result, [["data"]])
        mock_sleep.assert_not_called()

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_retries_on_429_then_succeeds(self, mock_get, mock_sleep):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = [["ok"]]

        mock_get.side_effect = [resp_429, resp_429, resp_ok]
        result = _binance_request("https://x/klines", {}, max_retries=4)
        self.assertEqual(result, [["ok"]])
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_429_exhausts_retries_raises(self, mock_get, mock_sleep):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        mock_get.return_value = resp_429

        with self.assertRaises(RuntimeError) as ctx:
            _binance_request("https://x/endpoint", {}, max_retries=3)
        self.assertIn("endpoint=https://x/endpoint", str(ctx.exception))
        self.assertIn("retries=3", str(ctx.exception))

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_respects_retry_after_header(self, mock_get, mock_sleep):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "2"}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = []

        mock_get.side_effect = [resp_429, resp_ok]
        _binance_request("https://x/klines", {}, max_retries=3)
        mock_sleep.assert_called_once_with(2.0)

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_retry_after_capped_by_max_sleep(self, mock_get, mock_sleep):
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "999"}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = []

        mock_get.side_effect = [resp_429, resp_ok]
        _binance_request("https://x/klines", {}, max_retries=3)
        # Should be capped at _MAX_SLEEP (30)
        mock_sleep.assert_called_once_with(30.0)

    @patch("pipeline.features.core_data_sources.time.sleep")
    @patch("pipeline.features.core_data_sources.requests.get")
    def test_retries_on_500(self, mock_get, mock_sleep):
        resp_500 = MagicMock()
        resp_500.status_code = 500
        resp_500.headers = {}

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = [["ok"]]

        mock_get.side_effect = [resp_500, resp_ok]
        result = _binance_request("https://x/klines", {}, max_retries=3)
        self.assertEqual(result, [["ok"]])

    @patch("pipeline.features.core_data_sources.requests.get")
    def test_raises_immediately_on_4xx(self, mock_get):
        resp = MagicMock()
        resp.status_code = 403
        resp.raise_for_status.side_effect = Exception("Forbidden")
        mock_get.return_value = resp

        with self.assertRaises(Exception):
            _binance_request("https://x/klines", {}, max_retries=3)


# -----------------------------------------------------------------------
# Chunk ranges tests
# -----------------------------------------------------------------------

class TestChunkRanges(unittest.TestCase):
    def test_small_range_no_chunking(self):
        chunks = _chunk_ranges(0, 100_000, "1m", 1000)
        self.assertEqual(chunks, [(0, 100_000)])

    def test_large_range_produces_chunks(self):
        # 1h interval, limit=1000 → chunk_ms = 3_600_000 * 1000 * 3
        one_year_ms = 365 * 86_400_000
        chunks = _chunk_ranges(0, one_year_ms, "1h", 1000)
        self.assertGreater(len(chunks), 1)
        # Chunks should cover full range
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][1], one_year_ms)
        # No gaps
        for i in range(len(chunks) - 1):
            self.assertEqual(chunks[i][1], chunks[i + 1][0])

    def test_unknown_interval_returns_single_chunk(self):
        chunks = _chunk_ranges(0, 1_000_000, "3M", 1000)
        self.assertEqual(chunks, [(0, 1_000_000)])


if __name__ == "__main__":
    unittest.main()
