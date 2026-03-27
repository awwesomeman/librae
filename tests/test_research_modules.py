import json
import tempfile
import unittest
from pathlib import Path

from librae import run_strict_protocol, Periods
from librae import run_walkforward, WFWindow
from librae import run_stability
from scripts.reporting.render_report import render_brief


class TestResearchModules(unittest.TestCase):
    def test_run_strict_protocol_selects_on_train_only(self):
        def backtest_fn(start, end, **params):
            # score prefers higher p in train
            p = params.get("p", 0)
            if start == "train_s":
                return {"trades": 20, "ann_sharpe": p, "mdd": 0.1, "ann_return": 0.1}
            if start == "val_s":
                return {"trades": 10, "ann_sharpe": 0.5, "mdd": 0.2, "ann_return": 0.05}
            return {"trades": 8, "ann_sharpe": 0.3, "mdd": 0.15, "ann_return": 0.04}

        periods = Periods("train_s", "train_e", "val_s", "val_e", "oos_s", "oos_e")
        out = run_strict_protocol(backtest_fn, [{"p": 1}, {"p": 2}], periods)
        self.assertEqual(out["chosen_params"], {"p": 2})
        self.assertIn("oos_final_once", out)

    def test_run_walkforward_summary(self):
        def backtest_fn(start, end, **params):
            p = params.get("p", 1)
            return {
                "trades": 12,
                "ann_sharpe": p,
                "mdd": 0.05,
                "ann_return": 0.1 if p > 0 else -0.1,
                "avg_ret": 0.01,
            }

        windows = [
            WFWindow("t1", "t2", "o1", "o2"),
            WFWindow("t3", "t4", "o3", "o4"),
        ]
        out = run_walkforward(backtest_fn, windows, [{"p": 1}])
        self.assertEqual(out["summary"]["windows_total"], 2)
        self.assertEqual(out["summary"]["windows_scored"], 2)

    def test_run_stability_schema(self):
        def backtest_fn(start, end, **params):
            return {
                "trades": 9,
                "ann_return": 0.12,
                "ann_sharpe": 1.5,
                "mdd": 0.08,
                "pf": 1.9,
            }

        out = run_stability(backtest_fn, "s", "e", [{"a": 1}, {"a": 2}])
        self.assertEqual(out["summary"]["cases_total"], 2)
        self.assertEqual(out["summary"]["robust_cases"], 2)

    def test_render_brief_contains_required_metrics(self):
        data = {
            "strategy": "X",
            "train": {
                "trades": 10,
                "win_rate": 0.6,
                "avg_ret": 0.01,
                "pf": 1.7,
                "ann_return": 0.12,
                "ann_sharpe": 1.4,
                "ann_vol": 0.2,
                "mdd": 0.08,
            },
            "oos_final_once": {
                "trades": 8,
                "win_rate": 0.5,
                "avg_ret": 0.008,
                "pf": 1.3,
                "ann_return": 0.09,
                "ann_sharpe": 1.0,
                "ann_vol": 0.18,
                "mdd": 0.07,
            },
        }
        text = render_brief(data)
        self.assertIn("交易次數", text)
        self.assertIn("勝率", text)
        self.assertIn("平均報酬率", text)


if __name__ == "__main__":
    unittest.main()
