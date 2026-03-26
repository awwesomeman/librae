"""
Integration smoke tests: verify layered module imports and monitor profile files.
No network, no side effects.
"""
import importlib
import json
import os
import unittest

MONITOR_PROFILES_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "scripts",
    "monitor",
    "monitor_profiles",
)

LAYERED_MODULES = [
    "quant_lab.backtest.runners",
    "quant_lab.backtest.scoring",
    "quant_lab.backtest.adapter",
    "scripts.etl.core_data_sources",
    "scripts.etl.core_features",
    "scripts.monitor.monitor_core",
    "scripts.monitor.monitor_run",
    "scripts.reporting.render_report",
    "scripts.reporting.save_reports",
]


class TestLayeredImports(unittest.TestCase):
    def test_all_modules_importable(self):
        for mod in LAYERED_MODULES:
            with self.subTest(module=mod):
                m = importlib.import_module(mod)
                self.assertIsNotNone(m)


class TestMonitorProfileFiles(unittest.TestCase):
    def _profile_paths(self):
        profile_dir = os.path.normpath(MONITOR_PROFILES_DIR)
        self.assertTrue(
            os.path.isdir(profile_dir),
            f"monitor_profiles directory not found: {profile_dir}",
        )
        return [
            os.path.join(profile_dir, f)
            for f in os.listdir(profile_dir)
            if f.endswith(".json")
        ]

    def test_profiles_exist(self):
        paths = self._profile_paths()
        self.assertGreater(len(paths), 0, "No .json profile files found in monitor_profiles/")

    def test_profiles_parse_as_json(self):
        for path in self._profile_paths():
            with self.subTest(file=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertIsInstance(data, dict)

    def test_profiles_have_required_keys(self):
        # "symbol" is Binance-specific; only "kind", "strategy", "params" are universal
        required = {"kind", "strategy", "params"}
        for path in self._profile_paths():
            with self.subTest(file=os.path.basename(path)):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                missing = required - data.keys()
                self.assertFalse(missing, f"Profile {os.path.basename(path)} missing keys: {missing}")


if __name__ == "__main__":
    unittest.main()
