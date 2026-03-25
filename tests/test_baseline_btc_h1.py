"""Baseline BTC H1 regression test.

Pins deterministic SMA crossover backtest output on synthetic BTC H1 data
(seed=42, 720 bars). Any change to sample_data or the simple SMA crossover
logic will break these assertions, which is the intended behavior.
"""
from __future__ import annotations

import json
import tempfile

import pytest

from nautilus_lab.backtest.sample_data import (
    generate_btc_h1_ohlcv,
    run_simple_sma_crossover,
)
from nautilus_lab.backtest.adapter import metrics_dict_to_backtest_output
from nautilus_lab.backtest.persistence import save_backtest_output, load_backtest_output
from nautilus_lab.backtest.schema import (
    BacktestOutput,
    RUN_ID_PATTERN,
    VALID_SAMPLE_LABELS,
)


# -- Pinned baseline values (seed=42, fast=10, slow=30) --
EXPECTED_TRADES = 13
EXPECTED_WIN_RATE = pytest.approx(0.15384615384615385, abs=1e-9)
EXPECTED_MDD = pytest.approx(0.37907270052537334, abs=1e-9)
EXPECTED_EQUITY = pytest.approx(0.6209272994746267, abs=1e-9)
EXPECTED_AVG_PNL_POINTS = pytest.approx(-1943.2576923076917, abs=1e-4)
EXPECTED_PF = pytest.approx(0.07349755209319286, abs=1e-9)


@pytest.fixture
def btc_h1_data():
    return generate_btc_h1_ohlcv()


@pytest.fixture
def baseline_metrics(btc_h1_data):
    return run_simple_sma_crossover(btc_h1_data, fast_period=10, slow_period=30)


# ---------------------------------------------------------------------------
# Data reproducibility
# ---------------------------------------------------------------------------


class TestDataReproducibility:
    def test_shape(self, btc_h1_data):
        assert btc_h1_data.shape == (720, 6)

    def test_columns(self, btc_h1_data):
        assert list(btc_h1_data.columns) == [
            "timestamp", "open", "high", "low", "close", "volume",
        ]

    def test_first_close_deterministic(self, btc_h1_data):
        assert btc_h1_data["close"].iloc[0] == 60286.25
        assert btc_h1_data["open"].iloc[0] == 60_000.0

    def test_regeneration_identical(self):
        df1 = generate_btc_h1_ohlcv()
        df2 = generate_btc_h1_ohlcv()
        assert df1.equals(df2)


# ---------------------------------------------------------------------------
# Baseline regression
# ---------------------------------------------------------------------------


class TestBaselineRegression:
    def test_trade_count(self, baseline_metrics):
        assert baseline_metrics["trades"] == EXPECTED_TRADES

    def test_win_rate(self, baseline_metrics):
        assert baseline_metrics["win_rate"] == EXPECTED_WIN_RATE

    def test_max_drawdown(self, baseline_metrics):
        assert baseline_metrics["mdd"] == EXPECTED_MDD

    def test_equity(self, baseline_metrics):
        assert baseline_metrics["equity"] == EXPECTED_EQUITY

    def test_avg_pnl_points(self, baseline_metrics):
        assert baseline_metrics["avg_pnl_points"] == EXPECTED_AVG_PNL_POINTS

    def test_profit_factor(self, baseline_metrics):
        assert baseline_metrics["pf"] == EXPECTED_PF

    def test_trade_details_present(self, baseline_metrics):
        assert len(baseline_metrics["trade_details"]) == EXPECTED_TRADES


# ---------------------------------------------------------------------------
# Contract v1: run_id format
# ---------------------------------------------------------------------------


class TestRunIdContract:
    def test_run_id_pattern_valid(self):
        valid_ids = [
            "sma_crossover-btcusdt-20240101t000000-abcd1234",
            "mean_reversion-ethusdt-20260325t120000-deadbeef",
        ]
        for rid in valid_ids:
            assert RUN_ID_PATTERN.match(rid), f"Should match: {rid}"

    def test_run_id_pattern_invalid(self):
        invalid_ids = [
            "UpperCase-btcusdt-20240101t000000-abcd1234",
            "no-timestamp-here",
            "",
            "sma-btcusdt-2024-abcd1234",
        ]
        for rid in invalid_ids:
            assert not RUN_ID_PATTERN.match(rid), f"Should not match: {rid}"

    def test_generated_run_id_matches_pattern(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="train",
        )
        assert RUN_ID_PATTERN.match(output.run_metadata.run_id)

    def test_validate_rejects_bad_run_id(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            run_id="bad-run-id",
        )
        with pytest.raises(ValueError, match="run_metadata.run_id must match pattern"):
            output.validate()


# ---------------------------------------------------------------------------
# Contract v1: sample labels (train/oos/live)
# ---------------------------------------------------------------------------


class TestSampleLabels:
    def test_valid_sample_labels(self):
        assert VALID_SAMPLE_LABELS == frozenset({"train", "validation", "oos", "live"})

    def test_backtest_output_with_each_sample(self, baseline_metrics):
        for sample in ("train", "validation", "oos", "live"):
            output = metrics_dict_to_backtest_output(
                baseline_metrics,
                strategy="sma_crossover_baseline",
                symbol="BTCUSDT",
                timeframe="H1",
                start="2024-01-01",
                end="2024-01-31",
                data_source="synthetic_seed42",
                sample=sample,
            )
            output.validate()
            assert output.run_metadata.sample == sample

    def test_invalid_sample_raises(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="bad_label",
        )
        with pytest.raises(ValueError, match="sample must be one of"):
            output.validate()


# ---------------------------------------------------------------------------
# Contract v1: trade records integration
# ---------------------------------------------------------------------------


class TestTradeRecordsIntegration:
    def test_adapter_populates_trade_records(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="train",
        )
        assert len(output.trades) == EXPECTED_TRADES

    def test_trade_record_fields(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="oos",
        )
        tr = output.trades[0]
        assert tr.symbol == "BTCUSDT"
        assert tr.side == "buy"
        assert tr.quantity == 1.0
        assert tr.gross_pnl == tr.exit_price - tr.entry_price
        assert tr.holding_bars is not None

    def test_trade_ids_unique(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="oos",
        )
        trade_ids = [t.trade_id for t in output.trades]
        assert len(trade_ids) == len(set(trade_ids))


# ---------------------------------------------------------------------------
# Contract v1: to_dict / roundtrip
# ---------------------------------------------------------------------------


class TestContractV1Roundtrip:
    def test_to_dict_keys(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="oos",
        )
        d = output.to_dict()
        assert "run_metadata" in d
        assert "equity_curve" in d
        assert "trades" in d
        assert "metrics" in d
        assert d["run_metadata"]["sample"] == "oos"

    def test_to_dict_datetimes_are_strings(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="train",
        )
        d = output.to_dict()
        assert isinstance(d["run_metadata"]["start_ts"], str)
        assert isinstance(d["run_metadata"]["end_ts"], str)
        assert isinstance(d["run_metadata"]["run_ts"], str)

    def test_to_dict_json_serializable(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="oos",
        )
        text = json.dumps(output.to_dict())
        assert isinstance(text, str)

    def test_roundtrip_persistence_with_trades(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="oos",
        )
        output.validate()

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = save_backtest_output(output, tmpdir)
            loaded = load_backtest_output(paths["json"])

        assert loaded.run_metadata.sample == "oos"
        assert loaded.metrics.trades == EXPECTED_TRADES
        assert loaded.metrics.avg_pnl_points == EXPECTED_AVG_PNL_POINTS
        assert len(loaded.trades) == EXPECTED_TRADES
        assert loaded.trades[0].symbol == "BTCUSDT"

    def test_roundtrip_preserves_trade_pnl(self, baseline_metrics):
        output = metrics_dict_to_backtest_output(
            baseline_metrics,
            strategy="sma_crossover_baseline",
            symbol="BTCUSDT",
            timeframe="H1",
            start="2024-01-01",
            end="2024-01-31",
            data_source="synthetic_seed42",
            sample="train",
        )
        output.validate()

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = save_backtest_output(output, tmpdir)
            loaded = load_backtest_output(paths["json"])

        for orig, loaded_tr in zip(output.trades, loaded.trades):
            assert orig.gross_pnl == pytest.approx(loaded_tr.gross_pnl, abs=1e-6)
            assert orig.entry_price == pytest.approx(loaded_tr.entry_price, abs=1e-6)
