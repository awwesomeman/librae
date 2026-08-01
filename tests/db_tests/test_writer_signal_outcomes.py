"""Tests for signal_events write path in timescale_writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.backtest.cache import build_backtest_cache_key
from librae.backtest.schema import StrategyMetrics
from librae.core.run_config import RunConfig
from librae.db.timescale_writer import (
    _claim_backtest_cache_key,
    save_backtest_output,
    save_signal_results,
    save_strategy_results,
    write_equity_curve_point,
    write_ohlcv,
    write_run_metadata,
    write_signal_event,
    write_strategy_performance,
    write_trade_event,
)

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
        config_hash="config-a",
        backtest_revision="revision-a",
        backtest_cache_key="cache-a",
        cur=cursor,
    )

    values = cursor.execute.call_args.args[1]
    assert json.loads(values[10]) == {"window": 20}
    assert json.loads(values[11]) == {
        "default_fill_price": "open",
        "max_bar_volume_participation_rate": 0.1,
    }
    assert json.loads(values[12]) == {"max_drawdown_rate": 0.2}
    assert values[13:] == ("config-a", "revision-a", "cache-a")


class TestBacktestCacheKeyClaim:
    def test_missing_cache_key_disables_deduplication(self) -> None:
        cursor = MagicMock()

        assert (
            _claim_backtest_cache_key(
                cursor,
                None,
                "new-run",
                replace_existing=False,
            )
            is True
        )
        cursor.execute.assert_not_called()

    def test_duplicate_run_is_skipped_after_transaction_lock(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = ("canonical-run",)

        claimed = _claim_backtest_cache_key(
            cursor,
            "same-config",
            "racing-run",
            replace_existing=False,
        )

        assert claimed is False
        assert "pg_advisory_xact_lock" in cursor.execute.call_args_list[0].args[0]
        assert cursor.execute.call_args_list[0].args[1] == ("same-config",)
        assert all(
            "DELETE FROM backtest_runs" not in call.args[0]
            for call in cursor.execute.call_args_list
        )

    def test_force_recompute_replaces_canonical_run_under_lock(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = ("canonical-run",)

        claimed = _claim_backtest_cache_key(
            cursor,
            "same-config",
            "forced-run",
            replace_existing=True,
        )

        assert claimed is True
        cursor.execute.assert_any_call(
            "DELETE FROM backtest_runs WHERE run_id = %s",
            ("canonical-run",),
        )

    def test_same_run_id_remains_idempotent(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = ("same-run",)

        assert (
            _claim_backtest_cache_key(
                cursor,
                "same-config",
                "same-run",
                replace_existing=False,
            )
            is True
        )

    @patch("librae.db.timescale_writer.write_run_metadata")
    @patch("librae.db.timescale_writer._claim_backtest_cache_key", return_value=False)
    @patch("librae.db.timescale_writer.get_conn")
    def test_losing_backtest_writer_exits_without_partial_rows(
        self,
        mock_conn_ctx,
        mock_claim,
        mock_write_metadata,
    ) -> None:
        connection = MagicMock()
        connection.__enter__ = MagicMock(return_value=connection)
        connection.__exit__ = MagicMock(return_value=False)
        mock_conn_ctx.return_value = connection
        output = MagicMock()
        output.run_metadata.run_id = "racing-run"

        counts = save_backtest_output(
            output,
            config_hash="same-config",
            backtest_revision="revision-a",
        )

        assert counts == {"backtest_runs": 0}
        cache_key = build_backtest_cache_key("same-config", "revision-a")
        mock_claim.assert_called_once_with(
            connection.cursor.return_value,
            cache_key,
            "racing-run",
            replace_existing=False,
        )
        mock_write_metadata.assert_not_called()


def test_strategy_performance_sql_matches_account_metric_values() -> None:
    cursor = MagicMock()
    metrics = StrategyMetrics(total_return=0.1)

    write_strategy_performance(
        "run-1",
        "account-a",
        "USD",
        100.0,
        110.0,
        10.0,
        metrics,
        cur=cursor,
    )

    sql, values = cursor.execute.call_args.args
    assert sql.count("%s") == len(values) == 28
    assert values[:6] == ("run-1", "account-a", "USD", 100.0, 110.0, 10.0)


class TestWriteEquityCurvePoint:
    """write_equity_curve_point single-row upsert."""

    @patch("librae.db.timescale_writer.get_conn")
    def test_on_conflict_updates_every_inserted_column(self, mock_conn_ctx):
        """Every mutable engine-owned equity field is updated on conflict."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        write_equity_curve_point(
            ts=datetime(2024, 6, 1, tzinfo=UTC),
            run_id="test-run-001",
            account_id="default",
            currency="USD",
            equity=105_000.0,
            drawdown=-0.02,
            period_return=0.01,
            strategy="test_strat",
        )

        sql = mock_cur.execute.call_args[0][0]
        assert "ON CONFLICT" in sql
        for col in (
            "currency",
            "equity",
            "drawdown",
            "period_return",
            "gross_exposure",
            "net_exposure",
            "concentration",
            "turnover",
            "exposed",
            "strategy",
        ):
            assert f"{col}=EXCLUDED.{col}" in sql


@patch("librae.db.timescale_writer.get_conn")
def test_write_trade_event_sql_matches_persisted_cost_fields(mock_conn_ctx) -> None:
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    mock_conn_ctx.return_value = mock_conn

    write_trade_event(
        event_id="event-1",
        run_id="run-1",
        account_id="alpha",
        currency="USD",
        strategy="test",
        mode="backtest",
        timeframe="H1",
        ts=datetime(2024, 6, 1, tzinfo=UTC),
        symbol="TEST",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=110.0,
        entry_price=100.0,
        remaining_quantity=0.0,
        notional=110.0,
        commission=0.2,
        entry_commission=0.1,
    )

    sql, values = mock_cur.execute.call_args.args
    assert sql.count("%s") == len(values) == 27
    assert "entry_commission, entry_slippage, entry_tax" in sql
    assert values[19:22] == (0.1, None, None)


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


def test_write_ohlcv_rejects_invalid_instrument_type() -> None:
    frame = pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [10.0],
        },
        index=pd.DatetimeIndex([datetime(2024, 6, 1, tzinfo=UTC)], name="ts"),
    )

    with pytest.raises(ValueError, match="instrument_type"):
        write_ohlcv(frame, "BTCUSDT", "H1", "test", instrument_type="daily")


class TestWriteSignalEvent:
    """write_signal_event single-row upsert."""

    @patch("librae.db.timescale_writer.get_conn")
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

    @patch("librae.db.timescale_writer.get_conn")
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

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=20)
    @patch("librae.db.timescale_writer.save_backtest_output", return_value={"backtest_runs": 1})
    def test_extracts_signals_and_calls_writer(self, mock_write_bt, mock_write_ohlcv):
        df, _symbol = self._make_featured_df()
        mock_output = MagicMock()

        counts = save_strategy_results(
            mock_output,
            df,
            _test_cfg(),
            backtest_revision="revision-a",
        )

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
            "live_order_timeout_seconds": None,
            "warmup_periods": 720,
        }
        assert call_kwargs.kwargs["replace_existing"] is False
        assert call_kwargs.kwargs["backtest_revision"] == "revision-a"

        assert counts["ohlcv"] == 20

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=20)
    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_force_recompute_replaces_existing_hash(self, mock_write_bt, mock_write_ohlcv):
        df, _symbol = self._make_featured_df()

        save_strategy_results(
            MagicMock(),
            df,
            _test_cfg(),
            replace_existing=True,
            backtest_revision="revision-a",
        )

        assert mock_write_bt.call_args.kwargs["replace_existing"] is True

    def test_force_recompute_requires_revision(self):
        df, _symbol = self._make_featured_df()

        with pytest.raises(ValueError, match="requires backtest_revision"):
            save_strategy_results(
                MagicMock(),
                df,
                _test_cfg(),
                replace_existing=True,
            )

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=20)
    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
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

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=10)
    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
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

    @patch("librae.db.timescale_writer.write_run_metadata")
    @patch("librae.db.timescale_writer._claim_backtest_cache_key", return_value=False)
    @patch("librae.db.timescale_writer.write_ohlcv")
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_losing_config_hash_writer_exits_without_partial_rows(
        self,
        mock_conn_ctx,
        mock_exec_values,
        mock_ohlcv,
        mock_claim,
        mock_write_metadata,
    ) -> None:
        connection = MagicMock()
        connection.__enter__ = MagicMock(return_value=connection)
        connection.__exit__ = MagicMock(return_value=False)
        mock_conn_ctx.return_value = connection
        index = pd.date_range("2024-01-01", periods=1, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [10.0],
                "entry_signal": [1.0],
            },
            index=index,
        )
        config = _test_cfg()

        counts = save_signal_results(
            df,
            "BTCUSDT",
            "H1",
            "test_strategy",
            "binance_spot",
            run_id="racing-run",
            config=config,
            backtest_revision="revision-a",
        )

        assert counts == {"backtest_runs": 0}
        cache_key = build_backtest_cache_key(config.config_hash, "revision-a")
        mock_claim.assert_called_once_with(
            connection.cursor.return_value,
            cache_key,
            "racing-run",
            replace_existing=False,
        )
        mock_write_metadata.assert_not_called()
        mock_exec_values.assert_not_called()
        mock_ohlcv.assert_not_called()

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=10)
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
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

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=10)
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
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

    @patch("librae.db.timescale_writer.write_ohlcv", return_value=0)
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
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
