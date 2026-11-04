"""Tests for signal_events write path in timescale_writer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.backtest.cache import build_backtest_cache_key
from librae.backtest.schema import StrategyMetrics
from librae.core.market_data import MarketDataSubscription
from librae.core.run_config import RunConfig
from librae.db.timescale_writer import (
    _claim_backtest_cache_key,
    save_backtest_output,
    save_signal_results,
    save_strategy_results,
    write_equity_curve_point,
    write_run_bars,
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


def _subscription(
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "H1",
    calendar_id: str = "24/7",
    session_mode: str = "extended",
    data_source: str = "test",
    instrument_type: str = "spot",
) -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source=data_source,
        instrument_type=instrument_type,
    )


def _config_subscriptions(config: RunConfig) -> tuple[MarketDataSubscription, ...]:
    from librae.config.symbols import resolve_symbol
    from librae.core.market_data import subscription_from_instrument

    return tuple(
        subscription_from_instrument(
            resolve_symbol(config, symbol),
            timeframe=config.timeframe,
            session_mode=config.session_mode,
        )
        for symbol in config.symbols
    )


@patch("librae.db.timescale_writer.require_current_schema")
def test_run_metadata_persists_execution_policy_separately_from_params(_mock_schema):
    cursor = MagicMock()

    write_run_metadata(
        "run-1",
        "strategy",
        ["BTCUSDT"],
        "H1",
        "backtest",
        data_source="run-default",
        data_source_by_symbol={"BTCUSDT": "instrument-source"},
        session_mode="regular",
        primary_subscriptions=(
            _subscription(
                session_mode="regular",
                data_source="instrument-source",
            ),
        ),
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
    sql = cursor.execute.call_args.args[0]
    assert json.loads(values[5]) == {"BTCUSDT": "instrument-source"}
    assert json.loads(values[6]) == [
        _subscription(session_mode="regular", data_source="instrument-source").to_dict()
    ]
    assert values[7] == "regular"
    assert json.loads(values[13]) == {"window": 20}
    assert json.loads(values[14]) == {
        "default_fill_price": "open",
        "max_bar_volume_participation_rate": 0.1,
    }
    assert json.loads(values[15]) == {"max_drawdown_rate": 0.2}
    assert values[16:] == ("config-a", "revision-a", "cache-a", None)
    assert "backtest_runs.primary_subscriptions = EXCLUDED.primary_subscriptions" in sql


@patch("librae.db.timescale_writer.require_current_schema")
def test_run_metadata_rejects_existing_different_immutable_identity(_mock_schema) -> None:
    cursor = MagicMock(rowcount=0)

    with pytest.raises(ValueError, match="different immutable identity"):
        write_run_metadata(
            "reused-run",
            "strategy",
            ["BTCUSDT"],
            "H1",
            "backtest",
            data_source="test",
            primary_subscriptions=(_subscription(),),
            cur=cursor,
        )


@patch("librae.db.timescale_writer.require_current_schema")
def test_live_run_metadata_persists_sanitized_execution_identity(_mock_schema) -> None:
    cursor = MagicMock(rowcount=1)
    identity = {
        "broker": "ibkr",
        "environment": "paper",
        "endpoint": "gateway:7497",
        "account_fingerprint": "a" * 24,
    }

    write_run_metadata(
        "live-run",
        "strategy",
        ["MU"],
        "H1",
        "live",
        data_source="test",
        primary_subscriptions=(
            MarketDataSubscription(
                symbol="MU",
                timeframe="H1",
                calendar_id="XNYS",
                session_mode="extended",
                data_source="test",
                instrument_type="spot",
            ),
        ),
        execution_identity=identity,
        cur=cursor,
    )

    assert json.loads(cursor.execute.call_args.args[1][-1]) == identity


def test_live_run_metadata_requires_execution_identity() -> None:
    with pytest.raises(ValueError, match="requires an execution_identity"):
        write_run_metadata(
            "live-run",
            "strategy",
            ["MU"],
            "H1",
            "live",
            data_source="test",
            primary_subscriptions=(
                MarketDataSubscription(
                    symbol="MU",
                    timeframe="H1",
                    calendar_id="XNYS",
                    session_mode="extended",
                    data_source="test",
                    instrument_type="spot",
                ),
            ),
            cur=MagicMock(),
        )


def test_new_run_metadata_rejects_legacy_empty_subscription_sentinel() -> None:
    with pytest.raises(ValueError, match="primary_subscriptions is required"):
        write_run_metadata(
            "new-run",
            "strategy",
            ["BTCUSDT"],
            "H1",
            "backtest",
            data_source="test",
            primary_subscriptions=(),
            cur=MagicMock(),
        )


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
            strategy_name="test_strat",
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
            "strategy_name",
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
        strategy_name="test",
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
    assert sql.count("%s") == len(values) == 35
    assert "entry_commission, entry_slippage, entry_tax" in sql
    assert values[19:22] == (0.1, None, None)


def test_write_run_bars_requires_real_volume() -> None:
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
        write_run_bars("run-1", _subscription(), frame)


def test_write_run_bars_rejects_partial_scalar_identity() -> None:
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

    with pytest.raises(TypeError, match="MarketDataSubscription"):
        write_run_bars("run-1", "BTCUSDT", frame)  # type: ignore[arg-type]


def _one_bar(
    ts: datetime = datetime(2024, 6, 3, 13, tzinfo=UTC), **clocks: datetime
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [101.0],
            "low": [99.0],
            "close": [100.0],
            "volume": [10.0],
            **{name: [value] for name, value in clocks.items()},
        },
        index=pd.DatetimeIndex([ts], name="ts"),
    )


@patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
@patch("librae.db.timescale_writer.get_conn")
def test_write_run_bars_keys_rows_by_run_and_keeps_the_first_receipt(
    mock_conn_ctx, mock_exec_values
) -> None:
    """A version already stored is left alone: overwriting it would let a
    replayed delivery move when the run received that bar."""
    mock_exec_values.return_value = [(1,)]
    mock_conn = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn_ctx.return_value = mock_conn
    subscription = _subscription(
        symbol="AAPL",
        calendar_id="XNYS",
        session_mode="regular",
        data_source="ibkr",
    )
    available_at = datetime(2024, 6, 3, 15, tzinfo=UTC)
    observed_at = datetime(2024, 6, 3, 15, 0, 4, tzinfo=UTC)

    inserted = write_run_bars(
        "run-1",
        subscription,
        _one_bar(
            datetime(2024, 6, 3, 13, 30, tzinfo=UTC),
            available_at=available_at,
            observed_at=observed_at,
        ),
    )

    sql = mock_exec_values.call_args[0][1]
    row = mock_exec_values.call_args[0][2][0]
    assert "ON CONFLICT (run_id, symbol, ts, available_at) DO NOTHING" in sql
    assert "DO UPDATE" not in sql
    assert mock_exec_values.call_args.kwargs["fetch"] is True
    assert inserted == 1
    assert row[1] == "run-1"
    assert row[2:8] == tuple(subscription.to_dict().values())
    assert row[8:10] == (available_at, observed_at)


@patch("librae.db.timescale_writer.psycopg2.extras.execute_values", return_value=[])
@patch("librae.db.timescale_writer.get_conn")
def test_write_run_bars_without_a_receipt_time_stores_none(mock_conn_ctx, mock_exec_values):
    """A backtest replays its inputs; it observed nothing, so no time is invented."""
    mock_conn_ctx.return_value.__enter__.return_value = MagicMock()

    write_run_bars("run-1", _subscription(), _one_bar())

    row = mock_exec_values.call_args[0][2][0]
    assert row[9] is None
    assert row[8] == datetime(2024, 6, 3, 14, tzinfo=UTC)  # derived bar close


def test_write_run_bars_on_a_callers_cursor_opens_no_connection() -> None:
    cursor = MagicMock()
    with (
        patch("librae.db.timescale_writer.get_conn") as get_conn,
        patch("librae.db.timescale_writer.psycopg2.extras.execute_values", return_value=[]) as ev,
    ):
        write_run_bars("run-1", _subscription(), _one_bar(), cur=cursor)

    get_conn.assert_not_called()
    assert ev.call_args[0][0] is cursor


def test_write_run_bars_refuses_a_naive_receipt_time() -> None:
    with pytest.raises(ValueError, match="Timezone-naive"):
        write_run_bars("run-1", _subscription(), _one_bar(observed_at=datetime(2024, 6, 3, 15)))


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
            strategy_name="test_strat",
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
        assert (
            "ON CONFLICT (ts, run_id, strategy_name, symbol, mode, timeframe, signal_type)" in sql
        )

    @patch("librae.db.timescale_writer.get_conn")
    def test_accepts_cursor(self, mock_conn_ctx):
        """When cur is provided, uses it directly without opening connection."""
        mock_cur = MagicMock()
        ts = datetime(2024, 6, 1, tzinfo=UTC)

        write_signal_event(
            ts=ts,
            run_id="test-run-001",
            strategy_name="s",
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

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={"backtest_runs": 1})
    def test_extracts_signals_and_calls_writer(self, mock_write_bt):
        df, _symbol = self._make_featured_df()
        mock_output = MagicMock()
        config = _test_cfg()
        mock_output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        counts = save_strategy_results(
            mock_output,
            df,
            config,
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
            "max_rebalance_delay_bars": 0,
            "rebalance_residual_policy": "discard",
            "live_order_timeout_seconds": None,
            "warmup_periods": 720,
        }
        assert call_kwargs.kwargs["replace_existing"] is False
        assert call_kwargs.kwargs["backtest_revision"] == "revision-a"

        assert counts == {"backtest_runs": 1}

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_force_recompute_replaces_existing_hash(self, mock_write_bt):
        df, _symbol = self._make_featured_df()
        config = _test_cfg()
        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        save_strategy_results(
            output,
            df,
            config,
            replace_existing=True,
            backtest_revision="revision-a",
        )

        assert mock_write_bt.call_args.kwargs["replace_existing"] is True

    def test_force_recompute_requires_revision(self):
        df, _symbol = self._make_featured_df()
        config = _test_cfg()
        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        with pytest.raises(ValueError, match="requires backtest_revision"):
            save_strategy_results(
                output,
                df,
                config,
                replace_existing=True,
            )

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_excludes_nan_and_zero_signals(self, mock_write_bt):
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

        config = _test_cfg()
        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)
        save_strategy_results(output, df, config)

        signal_series = mock_write_bt.call_args.kwargs["signal_series_by_symbol"]["BTCUSDT"]
        # Should keep: 1.0, -1.0, 1.0, -0.5, 1.0 (5 values, excluding NaN and 0)
        assert len(signal_series) == 5
        assert 0.0 not in signal_series.values

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_stored_bars_cover_every_configured_symbol(self, mock_write_bt):
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

        config = _test_cfg(
            symbols=["AAA", "BBB"],
            instrument_overrides={
                symbol: {
                    "instrument_type": "spot",
                    "currency": "USDT",
                    "data_adapter": "crypto",
                    "calendar_id": "24/7",
                }
                for symbol in ("AAA", "BBB")
            },
            symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in ("AAA", "BBB")},
        )
        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        save_strategy_results(output, df, config, store_bars=True)

        bars = mock_write_bt.call_args.kwargs["bars_by_symbol"]
        assert set(bars) == {"AAA", "BBB"}
        # Bars only: a feature or signal column is not a bar the run saw.
        assert list(bars["BBB"].columns) == ["open", "high", "low", "close", "volume"]
        assert bars["BBB"].index.name == "ts"
        assert len(bars["BBB"]) == 3
        assert "BBB" in mock_write_bt.call_args.kwargs["signal_series_by_symbol"]

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_input_bars_are_not_copied_by_default(self, mock_write_bt):
        """The run's metadata names the exact series and range it used;
        copying the inputs is for a chart and must be asked for."""
        df, _symbol = self._make_featured_df()
        config = _test_cfg()
        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        save_strategy_results(output, df, config)

        assert mock_write_bt.call_args.kwargs["bars_by_symbol"] is None

    @patch("librae.db.timescale_writer.save_backtest_output", return_value={})
    def test_persists_per_instrument_data_source_identity(
        self,
        mock_write_bt,
    ) -> None:
        df, symbol = self._make_featured_df(n=3)
        config = _test_cfg(
            data_source="multi",
            instrument_overrides={
                symbol: {
                    "data_source": "ibkr",
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "venue_symbol": "BTC/USDT",
                    "security_type": "STK",
                }
            },
        )

        output = MagicMock()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)

        save_strategy_results(output, df, config)

        assert mock_write_bt.call_args.kwargs["data_source_by_symbol"] == {symbol: "ibkr"}


class TestSaveSignalResults:
    """save_signal_results writes signals independently of backtest."""

    @patch("librae.db.timescale_writer.write_run_metadata")
    @patch("librae.db.timescale_writer._claim_backtest_cache_key", return_value=False)
    @patch("librae.db.timescale_writer.write_run_bars")
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_losing_config_hash_writer_exits_without_partial_rows(
        self,
        mock_conn_ctx,
        mock_exec_values,
        mock_run_bars,
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
            store_bars=True,
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
        mock_run_bars.assert_not_called()

    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_writes_signal_events_without_backtest(self, mock_conn_ctx, mock_exec_values):
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

        counts = save_signal_results(
            df,
            "BTCUSDT",
            "H1",
            "test_strategy",
            "binance_spot",
            primary_subscription=_subscription(data_source="binance_spot"),
        )

        assert counts == {"signal_events": 4}  # indices 0,5,10,15
        # Verify DELETE was called
        assert mock_cur.execute.call_count >= 1
        # Verify batch INSERT was called
        mock_exec_values.assert_called_once()

    @patch("librae.db.timescale_writer.require_current_schema")
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_delete_scoped_to_own_run_id(self, mock_conn_ctx, mock_exec_values, _mock_schema):
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

        save_signal_results(
            df,
            "BTCUSDT",
            "H1",
            "test_strategy",
            "binance_spot",
            run_id="run-A",
            primary_subscription=_subscription(data_source="binance_spot"),
        )

        delete_call = next(
            c for c in mock_cur.execute.call_args_list if "DELETE FROM signal_events" in c.args[0]
        )
        sql, params = delete_call.args
        assert "run_id IS NOT DISTINCT FROM" in sql
        assert params[0] == "run-A"

    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_handles_multiindex_df(self, mock_conn_ctx, mock_exec_values):
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

        counts = save_signal_results(
            df,
            "BTCUSDT",
            "H1",
            "test_strategy",
            "binance_spot",
            primary_subscription=_subscription(data_source="binance_spot"),
        )

        # 1.0, -1.0, 1.0, 1.0, -0.5 = 5 non-zero non-NaN
        assert counts["signal_events"] == 5

    def test_storing_bars_needs_a_run(self) -> None:
        with pytest.raises(ValueError, match="store_bars=True requires run_id"):
            save_signal_results(
                pd.DataFrame(),
                "BTCUSDT",
                "H1",
                "test_strategy",
                "binance_spot",
                primary_subscription=_subscription(data_source="binance_spot"),
                store_bars=True,
            )

    @patch("librae.db.timescale_writer.require_current_schema")
    @patch("librae.db.timescale_writer.write_run_bars", return_value=5)
    @patch("librae.db.timescale_writer.psycopg2.extras.execute_values")
    @patch("librae.db.timescale_writer.get_conn")
    def test_stored_bars_join_the_run_in_its_transaction(
        self, mock_conn_ctx, _exec_values, mock_run_bars, _schema
    ) -> None:
        """run_bars rows need their run row, so they are written on the cursor
        that registered it, after the run's earlier bars are cleared."""
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn
        idx = pd.date_range("2024-01-01", periods=5, freq="1h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
                "entry_signal": 1.0,
            },
            index=idx,
        )
        subscription = _subscription(data_source="binance_spot")

        counts = save_signal_results(
            df,
            "BTCUSDT",
            "H1",
            "test_strategy",
            "binance_spot",
            run_id="run-A",
            primary_subscription=subscription,
            store_bars=True,
        )

        assert counts["run_bars"] == 5
        run_id, written_subscription, bars = mock_run_bars.call_args.args
        assert (run_id, written_subscription) == ("run-A", subscription)
        assert list(bars.columns) == ["open", "high", "low", "close", "volume"]
        assert mock_run_bars.call_args.kwargs["cur"] is mock_cur
        mock_cur.execute.assert_any_call("DELETE FROM run_bars WHERE run_id = %s", ("run-A",))


class TestSaveBacktestOutputBars:
    @staticmethod
    def _output(config: RunConfig) -> MagicMock:
        output = MagicMock()
        output.run_metadata.run_id = "run-1"
        output.run_metadata.timeframe = "H1"
        output.run_metadata.auxiliary_subscriptions = ()
        output.run_metadata.primary_subscriptions = _config_subscriptions(config)
        output.position_events = ()
        output.financing_cash_flows = ()
        output.account.equity_curve = ()
        return output

    @pytest.mark.parametrize(
        "bars_by_symbol",
        [{"ETHUSDT": pd.DataFrame()}, {}],
        ids=["outside the run", "missing a run symbol"],
    )
    def test_bars_must_cover_exactly_the_run_before_connecting(self, bars_by_symbol) -> None:
        """Bars for some symbols only would leave the others' positions
        silently unpriced in every mark."""
        output = self._output(_test_cfg())

        with (
            patch("librae.db.timescale_writer.get_conn") as get_conn,
            pytest.raises(ValueError, match="cover exactly the run symbols"),
        ):
            save_backtest_output(output, bars_by_symbol=bars_by_symbol)

        get_conn.assert_not_called()

    @patch("librae.db.timescale_writer.write_strategy_performance")
    @patch("librae.db.timescale_writer.write_run_metadata")
    @patch("librae.db.timescale_writer.write_run_bars", return_value=3)
    @patch("librae.db.timescale_writer.get_conn")
    def test_a_resave_replaces_the_runs_bars_in_one_transaction(
        self, mock_conn_ctx, mock_run_bars, _metadata, _performance
    ) -> None:
        """Re-saving a run replaces its rows: old bars left behind would
        disagree with the new results, and DO NOTHING would keep them."""
        connection = MagicMock()
        connection.__enter__ = MagicMock(return_value=connection)
        connection.__exit__ = MagicMock(return_value=False)
        mock_conn_ctx.return_value = connection
        cursor = connection.cursor.return_value
        config = _test_cfg()
        bars = pd.DataFrame({"open": [1.0]})

        counts = save_backtest_output(self._output(config), bars_by_symbol={"BTCUSDT": bars})

        statements = [call.args[0] for call in cursor.execute.call_args_list]
        assert "DELETE FROM run_bars WHERE run_id = %s" in statements
        assert counts["run_bars"] == 3
        run_id, subscription, written = mock_run_bars.call_args.args
        assert (run_id, subscription) == ("run-1", _config_subscriptions(config)[0])
        assert written is bars
        assert mock_run_bars.call_args.kwargs["cur"] is cursor
