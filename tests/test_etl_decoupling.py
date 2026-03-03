import unittest
import pandas as pd

from scripts.etl.core_features import resample_ohlcv, add_daily_trend_gate, add_multifactor_features
from scripts.etl.core_data_sources import normalize_ohlcv


class TestEtlDecoupling(unittest.TestCase):
    def test_normalize_ohlcv(self):
        raw = pd.DataFrame({
            "ts": ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"],
            "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [10, 20],
        })
        out = normalize_ohlcv(raw)
        self.assertEqual(list(out.columns), ["open", "high", "low", "close", "volume"])
        self.assertEqual(len(out), 2)

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


if __name__ == "__main__":
    unittest.main()
