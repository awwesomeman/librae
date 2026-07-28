"""Tests for signal_events write path in timescale_writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from db.timescale_writer import (
    save_signal_results,
    save_strategy_results,
    write_equity_curve_point,
    write_ohlcv,
    write_run_metadata,
    write_signal_event,
)
from librae.core.run_config import RunConfig
from tests.conftest import make_test_cfg


def _test_cfg(**overrides) -> RunConfig:
    overrides.setdefault("mode", "backtest")
    overrides.setdefault("params", {"a": 1})
    return make_test_cfg(**overrides)


def test_run_metadata_persists_execution_policy_separately_from_params():
    cursor = MagicMock()

    write_run_metadata(
        "run-1",
        "strategy",
        ["BTCUSDT"],
        "H1",
        "backtest",
        params={"window": 20},
        execution_policy={
            "default_fill_price": "open",
            "max_bar_volume_participation_rate": 0.1,
        },
        risk_policy={"max_drawdown_rate": 0.2},
        cur=cursor,
    )

    values = cursor.execute.call_args.args[1]
    assert json.loads(values[10]) == {"window": 20}
    assert json.loads(values[11]) == {
        "default_fill_price": "open",
        "max_bar_volume_participation_rate": 0.1,
    }
    assert json.loads(values[12]) == {"max_drawdown_rate": 0.2}


class TestWriteEquityCurvePoint:
    """write_equity_curve_point single-row upsert."""

    @patch("db.timescale_writer.get_conn")
    def test_on_conflict_updates_every_inserted_column(self, mock_conn_ctx):
        """Regression test: ON CONFLICT DO UPDATE previously omitted
        benchmark_equity/benchmark_period_return, so a re-write with
        different benchmark values would silently keep the first write's
        (or NULL) values forever."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        write_equity_curve_point(
            ts=datetime(2024, 6, 1, tzinfo=UTC),
            run_id="test-run-001",
            equity=105_000.0,
            drawdown=-0.02,
            period_return=0.01,
            benchmark_equity=101_000.0,
            benchmark_period_return=0.005,
            strategy="test_strat",
        )

        sql = mock_cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        for col in (
            "equity",
            "benchmark_equity",
            "drawdown",
            "period_return",
            "benchmark_period_return",
            "gross_exposure",
            "net_exposure",
            "concentration",
            "turnover",
            "strategy",
        ):
            assert f"{col}=EXCLUDED.{col}" in sql


def test_write_ohlcv_requires_real_volume() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
        },
        index=pd.DatetimeIndex([datetime(2024, 6, 1, tzinfo=UTC)], name="ts"),
    )

    with pytest.raises(ValueError, match="volume"):
        write_ohlcv(frame, "BTCUSDT", "H1", "test")


class TestWriteSignalEvent:
    """write_signal_event single-row upsert."""

    @patch("db.timescale_writer.get_conn")
    def test_inserts_row(self, mock_conn_ctx):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        ts = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
        write_signal_event(
            ts=ts,
            run_id="test-run-001",
            strategy="test_strat",
            symbol="BTCUSDT",
            mode="sim",
            timeframe="H1",
            signal_value=1.0,
            price=50000.0,
        )

        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "signal_events" in sql
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql
        # Regression test: run_id must be part of the dedup key, or two
        # different runs' signals for the same (ts, strategy, symbol, ...)
        # would collide and silently drop one run's row.
        assert "ON CONFLICT (ts, run_id, strategy, symbol, mode, timeframe, signal_type)" in sql

    @patch("db.timescale_writer.get_conn")
    def test_accepts_cursor(self, mock_conn_ctx):
        """When cur is provided, uses it directly without opening connection."""
        mock_cur = MagicMock()
        ts = datetime(2024, 6, 1, tzinfo=UTC)

        write_signal_event(
            ts=ts,
            run_id="test-run-001",
            strategy="s",
            symbol="S",
            mode="sim",
            timeframe="H1",
            signal_value=1.0,
            cur=mock_cur,
        )

        mock_cur.execute.assert_called_once()
        mock_conn_ctx.assert_not_called()


class TestPersistBacktest:
    """save_strategy_results extracts signals and writes to DB."""

    def _make_featured_df(self, n: int = 20) -> tuple[pd.DataFrame, str]:
        """Create a MultiIndex DataFrame mimicking strategy output."""
        symbol = "BTCUSDT"
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        mi = pd.MultiIndex.from_arrays(
            [[symbol] * n, idx],
            names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": [x + 1 for x in range(n)],
                "low": [max(0, x - 1) for x in range(n)],
                "close": [x + 0.5 for x in range(n)],
                "volume": [100.0] * n,
                "entry_signal": [i % 5 == 0 for i in range(n)],
            },
            index=mi,
        )
        return df, symbol

    @patch("db.timescale_writer.write_ohlcv", return_value=20)
    @patch("db.timescale_writer.save_backtest_output", return_value={"backtest_runs": 1})
    def test_extracts_signals_and_calls_writer(self, mock_write_bt, mock_write_ohlcv):
        df, _symbol = self._make_featured_df()
        mock_output = MagicMock()

        counts = save_strategy_results(mock_output, df, _test_cfg())

        mock_write_bt.assert_called_once()
        call_kwargs = mock_write_bt.call_args
        signal_series = call_kwargs.kwargs["signal_series_by_symbol"]["BTCUSDT"]
        assert signal_series is not None
        # entry_signal is True every 5th bar → 4 signals (indices 0,5,10,15)
        assert len(signal_series) == 4
        assert all(v == 1.0 for v in signal_series.values.tolist())
        assert call_kwargs.kwargs["execution_policy"] == {
            "default_fill_price": "open",
            "max_bar_volume_participation_rate": None,
            "adv_lookback_sessions": None,
            "max_adv_participation_rate": None,
        }

        assert counts["ohlcv"] == 20

    @patch("db.timescale_writer.write_ohlcv", return_value=20)
    @patch("db.timescale_writer.save_backtest_output", return_value={})
    def test_excludes_nan_and_zero_signals(self, mock_write_bt, mock_write_ohlcv):
        """NaN and 0 values are excluded from signal_series."""
        symbol = "BTCUSDT"
        n = 10
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        mi = pd.MultiIndex.from_arrays([[symbol] * n, idx], names=["symbol", "datetime"])
        signals = [1.0, 0.0, float("nan"), -1.0, 0.0, 1.0, float("nan"), 0.0, -0.5, 1.0]
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": range(n),
                "low": range(n),
                "close": range(n),
                "volume": [100] * n,
                "entry_signal": signals,
            },
            index=mi,
        )

        save_strategy_results(MagicMock(), df, _test_cfg())

        signal_series = mock_write_bt.call_args.kwargs["signal_series_by_symbol"]["BTCUSDT"]
        # Should keep: 1.0, -1.0, 1.0, -0.5, 1.0 (5 values, excluding NaN and 0)
        assert len(signal_series) == 5
        assert 0.0 not in signal_series.values

    @patch("db.timescale_writer.write_ohlcv", return_value=10)
    @patch("db.timescale_writer.save_backtest_output", return_value={})
    def test_persists_every_configured_symbol(self, mock_write_bt, mock_write_ohlcv):
        timestamps = pd.date_range("2024-01-01", periods=3, freq="1h", tz="UTC")
        index = pd.MultiIndex.from_product(
            [["AAA", "BBB"], timestamps],
            names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 1_000.0,
                "entry_signal": [1.0, 0.0, 0.0, 0.0, -1.0, 0.0],
            },
            index=index,
        )

        counts = save_strategy_results(
            MagicMock(),
            df,
            _test_cfg(symbols=["AAA", "BBB"]),
        )

        assert mock_write_ohlcv.call_count == 2
        assert {call.args[1] for call in mock_write_ohlcv.call_args_list} == {
            "AAA",
            "BBB",
        }
        assert "BBB" in mock_write_bt.call_args.kwargs["signal_series_by_symbol"]
        assert counts["ohlcv"] == 20


class TestSaveSignalResults:
    """save_signal_results writes signals independently of backtest."""

    @patch("db.timescale_writer.write_ohlcv", return_value=10)
    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_writes_signal_events_without_backtest(
        self, mock_conn_ctx, mock_exec_values, mock_ohlcv
    ):
        """Can write signals without BacktestOutput."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        n = 20
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": range(n),
                "low": range(n),
                "close": range(n),
                "volume": [100] * n,
                "entry_signal": [1.0 if i % 5 == 0 else 0.0 for i in range(n)],
            },
            index=idx,
        )

        counts = save_signal_results(df, "BTCUSDT", "H1", "test_strategy", "binance_spot")

        assert counts["signal_events"] == 4  # indices 0,5,10,15
        assert counts["ohlcv"] == 10
        # Verify DELETE was called
        assert mock_cur.execute.call_count >= 1
        # Verify batch INSERT was called
        mock_exec_values.assert_called_once()

    @patch("db.timescale_writer.write_ohlcv", return_value=10)
    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_delete_scoped_to_own_run_id(self, mock_conn_ctx, mock_exec_values, mock_ohlcv):
        """Regression test: re-running save_signal_results for the same
        (strategy, symbol, timeframe) over an overlapping date range must
        only clear its own run's prior signal_events rows, not silently
        delete/overwrite a different run's rows for that same window."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        n = 5
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": range(n),
                "low": range(n),
                "close": range(n),
                "volume": [100] * n,
                "entry_signal": [1.0] * n,
            },
            index=idx,
        )

        save_signal_results(df, "BTCUSDT", "H1", "test_strategy", "binance_spot", run_id="run-A")

        delete_call = next(
            c for c in mock_cur.execute.call_args_list if "DELETE FROM signal_events" in c.args[0]
        )
        sql, params = delete_call.args
        assert "run_id IS NOT DISTINCT FROM" in sql
        assert params[0] == "run-A"

    @patch("db.timescale_writer.write_ohlcv", return_value=0)
    @patch("db.timescale_writer.psycopg2.extras.execute_values")
    @patch("db.timescale_writer.get_conn")
    def test_handles_multiindex_df(self, mock_conn_ctx, mock_exec_values, mock_ohlcv):
        """Works with MultiIndex (symbol, datetime) DataFrames."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        n = 10
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        mi = pd.MultiIndex.from_arrays(
            [["BTCUSDT"] * n, idx],
            names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": range(n),
                "low": range(n),
                "close": range(n),
                "volume": [100] * n,
                "entry_signal": [1.0, 0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, -0.5],
            },
            index=mi,
        )

        counts = save_signal_results(df, "BTCUSDT", "H1", "test_strategy", "binance_spot")

        # 1.0, -1.0, 1.0, 1.0, -0.5 = 5 non-zero non-NaN
        assert counts["signal_events"] == 5
