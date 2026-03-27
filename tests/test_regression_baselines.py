"""
Regression baseline tests for the backtest framework layer.
All stubs are deterministic and offline; no network required.
"""
import unittest

from librae import run_strict_protocol, Periods
from librae import run_walkforward, WFWindow
from librae import run_stability


# ---------------------------------------------------------------------------
# Shared deterministic helpers
# ---------------------------------------------------------------------------

def _make_metrics(trades, ann_return, ann_sharpe, mdd, avg_ret=None, pf=None):
    m = {
        "trades": trades,
        "ann_return": ann_return,
        "ann_sharpe": ann_sharpe,
        "mdd": mdd,
    }
    if avg_ret is not None:
        m["avg_ret"] = avg_ret
    if pf is not None:
        m["pf"] = pf
    return m


# ---------------------------------------------------------------------------
# run_strict_protocol regression
# ---------------------------------------------------------------------------

STRICT_PARAM_GRID = [
    {"fast": 5, "slow": 20},   # "weak" param – lower sharpe
    {"fast": 10, "slow": 30},  # "best" param – chosen by train
    {"fast": 3, "slow": 10},   # "noisy" param – high MDD
]

# Returns are keyed by (start, end, fast, slow) for full determinism.
_STRICT_RETURNS = {
    # ---- train ----
    ("2024-01-01", "2024-06-30", 5, 20):   _make_metrics(15, 0.10, 1.0, 0.08),
    ("2024-01-01", "2024-06-30", 10, 30):  _make_metrics(20, 0.18, 2.0, 0.05),
    ("2024-01-01", "2024-06-30", 3, 10):   _make_metrics(12, 0.05, 0.5, 0.25),
    # ---- validation ----
    ("2024-07-01", "2024-09-30", 10, 30):  _make_metrics(10, 0.12, 1.5, 0.07),
    # ---- OOS ----
    ("2024-10-01", "2024-12-31", 10, 30):  _make_metrics(8, 0.09, 1.2, 0.06),
}


def _strict_backtest_fn(start, end, fast, slow, cost=None):
    key = (start, end, fast, slow)
    if key not in _STRICT_RETURNS:
        return _make_metrics(0, 0, 0, 1)
    return dict(_STRICT_RETURNS[key])


class TestStrictProtocolRegression(unittest.TestCase):
    def setUp(self):
        self.periods = Periods(
            train_start="2024-01-01",
            train_end="2024-06-30",
            val_start="2024-07-01",
            val_end="2024-09-30",
            oos_start="2024-10-01",
            oos_end="2024-12-31",
        )
        self.result = run_strict_protocol(
            _strict_backtest_fn,
            STRICT_PARAM_GRID,
            self.periods,
            min_trades_train=10,
        )

    def test_no_error(self):
        self.assertNotIn("error", self.result)

    def test_chosen_params(self):
        # fast=10, slow=30 has highest score (sharpe=2.0, mdd=0.05 -> score=1.9)
        self.assertEqual(self.result["chosen_params"], {"fast": 10, "slow": 30})

    def test_train_summary_fields(self):
        train = self.result["train"]
        self.assertEqual(train["trades"], 20)
        self.assertAlmostEqual(train["ann_return"], 0.18)
        self.assertAlmostEqual(train["ann_sharpe"], 2.0)
        self.assertAlmostEqual(train["mdd"], 0.05)

    def test_oos_summary_fields(self):
        oos = self.result["oos_final_once"]
        self.assertEqual(oos["trades"], 8)
        self.assertAlmostEqual(oos["ann_return"], 0.09)

    def test_protocol_key_present(self):
        self.assertIn("protocol", self.result)

    def test_validation_present(self):
        self.assertIn("validation", self.result)


# ---------------------------------------------------------------------------
# run_walkforward regression
# ---------------------------------------------------------------------------

_WF_RETURNS = {
    ("2024-01-01", "2024-02-28", 10, 30): _make_metrics(15, 0.12, 1.5, 0.06, avg_ret=0.006),
    ("2024-03-01", "2024-03-31", 10, 30): _make_metrics(8, 0.08, 1.0, 0.05, avg_ret=0.004),
    ("2024-01-01", "2024-02-28", 5, 20):  _make_metrics(5, 0.03, 0.4, 0.10, avg_ret=0.002),
    ("2024-03-01", "2024-03-31", 5, 20):  _make_metrics(4, 0.02, 0.3, 0.12, avg_ret=0.001),
    # test windows
    ("2024-03-01", "2024-03-31", 10, 30): _make_metrics(9, 0.10, 1.2, 0.05, avg_ret=0.005),
    ("2024-04-01", "2024-04-30", 10, 30): _make_metrics(7, 0.07, 0.9, 0.06, avg_ret=0.003),
}

WF_WINDOWS = [
    WFWindow("2024-01-01", "2024-02-28", "2024-03-01", "2024-03-31"),
    WFWindow("2024-03-01", "2024-03-31", "2024-04-01", "2024-04-30"),
]

WF_PARAM_GRID = [
    {"fast": 5, "slow": 20},
    {"fast": 10, "slow": 30},
]


def _wf_backtest_fn(start, end, fast, slow):
    key = (start, end, fast, slow)
    return dict(_WF_RETURNS.get(key, _make_metrics(0, 0, 0, 1)))


class TestWalkforwardRegression(unittest.TestCase):
    def setUp(self):
        self.result = run_walkforward(
            _wf_backtest_fn,
            WF_WINDOWS,
            WF_PARAM_GRID,
            min_trades_train=5,
        )
        self.summary = self.result["summary"]

    def test_windows_scored(self):
        # Both windows should score (both params produce trades >= 5 on train)
        self.assertEqual(self.summary["windows_scored"], 2)

    def test_weighted_avg_test_ret(self):
        # Window 1: avg_ret=0.005, trades=9; Window 2: avg_ret=0.003, trades=7
        # weighted = (0.005*9 + 0.003*7) / (9+7) = (0.045+0.021)/16 = 0.066/16 = 0.004125
        expected = (0.005 * 9 + 0.003 * 7) / (9 + 7)
        self.assertAlmostEqual(self.summary["weighted_avg_test_ret"], expected, places=7)

    def test_windows_total(self):
        self.assertEqual(self.summary["windows_total"], 2)


# ---------------------------------------------------------------------------
# run_stability regression
# ---------------------------------------------------------------------------

_STAB_RETURNS = {
    (5, 20):  _make_metrics(20, 0.15, 1.5, 0.08),   # robust (>0 return, mdd<0.15)
    (7, 25):  _make_metrics(18, 0.12, 1.2, 0.10),   # robust
    (10, 30): _make_metrics(15, 0.10, 1.0, 0.07),   # robust
    (3, 15):  _make_metrics(10, -0.02, -0.2, 0.20), # not robust (negative return)
    (12, 35): _make_metrics(8, 0.05, 0.5, 0.18),    # not robust (mdd >= 0.15)
}

STAB_PARAM_GRID = [
    {"fast": fast, "slow": slow}
    for (fast, slow) in _STAB_RETURNS
]


def _stab_backtest_fn(start, end, fast, slow):
    return dict(_STAB_RETURNS[(fast, slow)])


class TestStabilityRegression(unittest.TestCase):
    def setUp(self):
        self.result = run_stability(
            _stab_backtest_fn,
            "2024-01-01",
            "2024-12-31",
            STAB_PARAM_GRID,
        )
        self.summary = self.result["summary"]

    def test_cases_total(self):
        self.assertEqual(self.summary["cases_total"], 5)

    def test_cases_valid(self):
        self.assertEqual(self.summary["cases_valid"], 5)

    def test_robust_cases(self):
        # fast=5,slow=20 mdd=0.08 <0.15 ✓; fast=7,slow=25 mdd=0.10 ✓; fast=10,slow=30 mdd=0.07 ✓
        self.assertEqual(self.summary["robust_cases"], 3)

    def test_robust_ratio(self):
        self.assertAlmostEqual(self.summary["robust_ratio"], 3 / 5, places=7)


if __name__ == "__main__":
    unittest.main()
