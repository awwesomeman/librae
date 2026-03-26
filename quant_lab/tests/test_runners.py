"""Tests for quant_lab.backtest.runners — ported protocol runners."""
from __future__ import annotations

import pytest

from quant_lab.backtest.runners import (
    Periods,
    WFWindow,
    run_strict_protocol,
    run_stability,
    run_walkforward,
)
from quant_lab.backtest.schema import BacktestOutput
from quant_lab.backtest.scoring import REQUIRED_METRICS_KEYS, score, validate_metrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dummy_backtest(start, end, **params):
    return {
        "trades": 20,
        "win_rate": 0.5,
        "avg_ret": 0.002,
        "pf": 1.2,
        "equity": 1.04,
        "ann_return": 0.08,
        "ann_sharpe": 1.0,
        "ann_vol": 0.08,
        "mdd": 0.05,
    }


def _no_trade_backtest(start, end, **params):
    return {
        "trades": 0,
        "win_rate": 0.0,
        "avg_ret": 0.0,
        "pf": 0.0,
        "equity": 1.0,
        "ann_return": 0.0,
        "ann_sharpe": 0.0,
        "ann_vol": 0.0,
        "mdd": 0.0,
    }


PERIODS = Periods(
    "2024-01-01", "2024-06-30",
    "2024-07-01", "2024-09-30",
    "2024-10-01", "2024-12-31",
)


# ---------------------------------------------------------------------------
# Scoring tests
# ---------------------------------------------------------------------------


class TestScoring:
    def test_score_basic(self):
        m = {"ann_sharpe": 1.5, "mdd": 0.1}
        assert score(m) == pytest.approx(1.5 - 2.0 * 0.1)

    def test_score_none_defaults(self):
        m = {"ann_sharpe": None, "mdd": None}
        assert score(m) == pytest.approx(-9 - 2.0)

    def test_validate_metrics_passes(self):
        m = {"trades": 10, "ann_return": 0.1, "ann_sharpe": 1.0, "mdd": 0.05}
        validate_metrics(m)  # should not raise

    def test_validate_metrics_missing_key_raises(self):
        m = {"trades": 10, "ann_return": 0.1}
        with pytest.raises(ValueError, match="missing required keys"):
            validate_metrics(m)

    def test_required_keys_constant(self):
        assert set(REQUIRED_METRICS_KEYS) == {"trades", "ann_return", "ann_sharpe", "mdd"}


# ---------------------------------------------------------------------------
# Strict protocol tests
# ---------------------------------------------------------------------------


class TestRunStrictProtocol:
    def test_basic_run(self):
        result = run_strict_protocol(
            _dummy_backtest, [{"cost": 2.0}], PERIODS, min_trades_train=5
        )
        assert "chosen_params" in result
        assert "train" in result
        assert "validation" in result
        assert "oos_final_once" in result

    def test_no_valid_params(self):
        result = run_strict_protocol(
            _no_trade_backtest, [{"cost": 2.0}], PERIODS, min_trades_train=5
        )
        assert result == {"error": "no_valid_params_on_train"}

    def test_no_backtest_outputs_without_context(self):
        result = run_strict_protocol(
            _dummy_backtest, [{"cost": 2.0}], PERIODS, min_trades_train=5
        )
        assert "backtest_outputs" not in result

    def test_backtest_outputs_with_context(self):
        result = run_strict_protocol(
            _dummy_backtest,
            [{"cost": 2.0}],
            PERIODS,
            min_trades_train=5,
            strategy="TestStrategy",
            symbol="TEST",
            timeframe="H1",
        )
        assert "backtest_outputs" in result
        outputs = result["backtest_outputs"]
        for sample in ("train", "validation", "oos"):
            assert sample in outputs
            assert isinstance(outputs[sample], BacktestOutput)
            outputs[sample].validate()
            assert outputs[sample].run_metadata.sample == sample
        # All samples share the same run_id
        run_ids = {outputs[s].run_metadata.run_id for s in ("train", "validation", "oos")}
        assert len(run_ids) == 1

    def test_cost_stress(self):
        result = run_strict_protocol(
            _dummy_backtest,
            [{"cost": 2.0}],
            PERIODS,
            min_trades_train=5,
            cost_stress=[3.0, 5.0],
        )
        assert "3.0" in result["validation_cost_stress"]
        assert "5.0" in result["validation_cost_stress"]


# ---------------------------------------------------------------------------
# Walk-forward tests
# ---------------------------------------------------------------------------


class TestRunWalkforward:
    def test_basic_run(self):
        windows = [
            WFWindow("2024-01-01", "2024-03-31", "2024-04-01", "2024-06-30"),
            WFWindow("2024-04-01", "2024-06-30", "2024-07-01", "2024-09-30"),
        ]
        result = run_walkforward(_dummy_backtest, windows, [{"cost": 2.0}])
        assert "wf_results" in result
        assert "summary" in result
        assert result["summary"]["windows_total"] == 2
        assert result["summary"]["windows_scored"] == 2

    def test_no_valid_param_window(self):
        windows = [
            WFWindow("2024-01-01", "2024-03-31", "2024-04-01", "2024-06-30"),
        ]
        result = run_walkforward(_no_trade_backtest, windows, [{"cost": 2.0}])
        assert result["wf_results"][0]["status"] == "no_valid_param"
        assert result["summary"]["windows_scored"] == 0


# ---------------------------------------------------------------------------
# Stability tests
# ---------------------------------------------------------------------------


class TestRunStability:
    def test_basic_run(self):
        result = run_stability(
            _dummy_backtest,
            "2024-01-01",
            "2024-12-31",
            [{"cost": 1.0}, {"cost": 2.0}, {"cost": 3.0}],
        )
        assert "stability_results" in result
        assert result["summary"]["cases_total"] == 3
        assert result["summary"]["cases_valid"] == 3
        assert result["summary"]["robust_cases"] == 3
        assert result["summary"]["robust_ratio"] == pytest.approx(1.0)
