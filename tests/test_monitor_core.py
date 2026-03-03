import json
import tempfile
import unittest

import pandas as pd

from scripts.monitor_core import trendpullback_setup_ok, trendpullback_trigger, append_signal_log


class TestMonitorCore(unittest.TestCase):
    def test_setup_ok_true_case(self):
        setup_time = pd.Timestamp("2026-03-01 12:00:00")
        setup = pd.Series({"low": 99.9, "ema20": 100.0, "atr14": 1.0, "close": 102.0, "open": 100.0, "volume": 100.0, "vol_sma20": 90.0})
        prev = pd.Series({"high": 101.0})

        d1 = pd.DataFrame(index=[pd.Timestamp("2026-02-28")], data={"close": [110.0], "ema20": [100.0], "ema20_prev": [99.0]})
        self.assertTrue(trendpullback_setup_ok(setup, prev, d1, setup_time, pull=0.3, vol_ratio=0.9))

    def test_trigger_finds_breakout(self):
        idx = pd.date_range("2026-03-01 12:01:00", periods=30, freq="min")
        df = pd.DataFrame(index=idx, data={
            "open": [100 + i * 0.01 for i in range(30)],
            "high": [100 + i * 0.02 for i in range(30)],
            "low": [99.5 + i * 0.01 for i in range(30)],
            "close": [100 + i * 0.03 for i in range(30)],
            "volume": [10] * 30,
        })
        out = trendpullback_trigger(df, breakout_n=5, trigger_ema=10)
        self.assertIsNotNone(out)

    def test_append_signal_log_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = f"{d}/signals.jsonl"
            append_signal_log(
                log_file=path,
                strategy="S",
                stage="setup",
                status="ok",
                message="hello",
            )
            with open(path, "r", encoding="utf-8") as f:
                line = f.readline().strip()
            obj = json.loads(line)
            self.assertEqual(obj["strategy"], "S")
            self.assertEqual(obj["stage"], "setup")
            self.assertEqual(obj["status"], "ok")


if __name__ == "__main__":
    unittest.main()
