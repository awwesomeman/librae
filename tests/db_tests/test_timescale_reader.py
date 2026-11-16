"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.backtest.schema import RunMetadata, StrategyMetrics
from librae.core.market_data import MarketDataSubscription
from librae.db.timescale_reader import (
    get_run,
    get_run_by_backtest_cache_key,
    load_backtest_output,
    load_run_bars,
    row_to_strategy_metrics,
)


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


def _subscription(*, instrument_type: str = "spot") -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol="BTCUSDT",
        timeframe="H1",
        calendar_id="24/7",
        session_mode="regular",
        data_source="fixture",
        instrument_type=instrument_type,
    )


class TestGetRun:
    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_run_metadata_by_run_id(self, mock_conn_ctx):
        mock_cur = MagicMock()
        started_at = datetime(2026, 7, 29, tzinfo=UTC)
        ended_at = datetime(2026, 7, 30, tzinfo=UTC)
        run_at = datetime(2026, 7, 30, 1, tzinfo=UTC)
        mock_cur.fetchone.return_value = (
            "demo-20260729t1200-abcdef",
            "demo",
            ["BTCUSDT"],
            "1h",
            "fixture",
            started_at,
            ended_at,
            run_at,
            "backtest",
            "regular",
            json.dumps([_subscription().to_dict()]),
        )
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_run("demo-20260729t1200-abcdef")

        assert result == RunMetadata(
            run_id="demo-20260729t1200-abcdef",
            strategy_name="demo",
            symbols=("BTCUSDT",),
            timeframe="1h",
            data_source="fixture",
            started_at=started_at,
            ended_at=ended_at,
            run_at=run_at,
            mode="backtest",
            session_mode="regular",
            primary_subscriptions=(_subscription(),),
        )
        sql = mock_cur.execute.call_args[0][0]
        assert "WHERE run_id = %s" in sql

    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_none_when_not_found(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run("missing-run") is None


class TestLoadRunBars:
    @patch("librae.db.timescale_reader.pd.read_sql")
    @patch("librae.db.timescale_reader.get_conn")
    def test_reads_one_run_and_keeps_the_latest_version_of_each_bar(
        self, mock_conn_ctx, mock_read_sql
    ):
        """The table keeps each version of a bar; a chart wants one per bar, the
        one received last, since revised values can keep their available_at."""
        mock_conn_ctx.return_value = _mock_conn(MagicMock())
        mock_read_sql.return_value = pd.DataFrame(
            {
                "_time": ["2026-01-02T14:30:00-05:00"],
                "available_at": ["2026-01-02T21:05:00+01:00"],
                "observed_at": ["2026-01-02T21:05:03+01:00"],
            }
        )

        result = load_run_bars("run-1")

        sql = " ".join(mock_read_sql.call_args.args[0].split())
        assert "FROM run_bars WHERE run_id = %s" in sql
        assert "DISTINCT ON (symbol, ts)" in sql
        assert "ORDER BY symbol, ts, observed_at DESC NULLS LAST, available_at DESC" in sql
        assert mock_read_sql.call_args.kwargs["params"] == ["run-1"]
        for column in ("_time", "available_at", "observed_at"):
            assert str(result[column].dt.tz) == "UTC"

    @patch("librae.db.timescale_reader.pd.read_sql", return_value=pd.DataFrame())
    @patch("librae.db.timescale_reader.get_conn")
    def test_a_run_that_stored_no_bars_reads_empty(self, mock_conn_ctx, _read_sql):
        mock_conn_ctx.return_value = _mock_conn(MagicMock())

        assert load_run_bars("backtest-without-bars").empty


class TestRowToStrategyMetrics:
    def test_picks_only_strategy_metrics_fields(self):
        row = {
            # load_performance() columns that are not StrategyMetrics fields
            "run_id": "demo-20260729t1200-abcdef",
            "account_id": "main",
            "currency": "USDT",
            "initial_cash": 10_000.0,
            "final_equity": 10_100.0,
            "net_pnl": 100.0,
            "strategy": "demo",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            # StrategyMetrics fields
            "total_return": 0.01,
            "max_drawdown": -0.02,
            "trades": 3,
            "mean_period_return": 0.001,
            "period_volatility": 0.02,
            "period_downside_deviation": 0.01,
            "period_sharpe": 1.2,
            "period_sortino": 1.5,
            "positive_period_rate": 0.6,
            "win_rate": 0.5,
            "profit_factor": 1.8,
            "payoff_ratio": 1.1,
            "avg_trade_return": 0.005,
            "exposure_ratio": 0.4,
            "total_turnover": 3.0,
            "average_gross_exposure": 0.3,
            "max_gross_exposure": 0.6,
            "max_abs_net_exposure": 0.5,
            "max_concentration": 0.7,
            "total_commission": 1.0,
            "total_slippage": 0.5,
            "total_tax": 0.0,
        }

        metrics = row_to_strategy_metrics(row)

        assert metrics == StrategyMetrics(
            total_return=0.01,
            max_drawdown=-0.02,
            trades=3,
            mean_period_return=0.001,
            period_volatility=0.02,
            period_downside_deviation=0.01,
            period_sharpe=1.2,
            period_sortino=1.5,
            positive_period_rate=0.6,
            win_rate=0.5,
            profit_factor=1.8,
            payoff_ratio=1.1,
            avg_trade_return=0.005,
            exposure_ratio=0.4,
            total_turnover=3.0,
            average_gross_exposure=0.3,
            max_gross_exposure=0.6,
            max_abs_net_exposure=0.5,
            max_concentration=0.7,
            total_commission=1.0,
            total_slippage=0.5,
            total_tax=0.0,
        )


class TestGetRunByBacktestCacheKey:
    @patch("librae.db.timescale_reader.get_conn")
    def test_reads_the_jsonb_columns_psycopg2_already_decoded(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (
            "run-1",
            {"window": 20},
            {"warmup_periods": 720},
            None,
            "extended",
        )
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run_by_backtest_cache_key("cache-a") == {
            "run_id": "run-1",
            "params": {"window": 20},
            "execution_policy": {"warmup_periods": 720},
            "risk_policy": None,
            "session_mode": "extended",
        }


def _stored_output():
    """A real backtest output with an open, a close and a NULL-able field."""
    from librae import Backtest, Context, CostModel, OrderIntent, Strategy

    class OpenThenClose(Strategy):
        def on_bar(self, ctx: Context) -> list[OrderIntent]:
            if ctx.period_index == 0:
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
            if ctx.period_index == 2:
                return [OrderIntent(action="close", symbol=ctx.symbol)]
            return []

    timestamps = pd.date_range("2026-01-01", periods=6, freq="h", tz="UTC")
    data = pd.DataFrame(
        {
            "open": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
            "high": 106.0,
            "low": 99.0,
            "close": [101.0, 102.0, 103.0, 104.0, 105.0, 106.0],
            "volume": 1_000.0,
        },
        index=pd.MultiIndex.from_arrays(
            [["BTCUSDT"] * 6, timestamps], names=["symbol", "datetime"]
        ),
    )
    backtest = Backtest(data, OpenThenClose(), cost_model=CostModel.zero(), data_source="fixture")
    backtest.run()
    return backtest.build_output()


def _frame(records) -> pd.DataFrame:
    return pd.DataFrame([asdict(record) for record in records]).rename(columns={"ts": "_time"})


class TestLoadBacktestOutput:
    def test_rebuilds_the_stored_output(self):
        output = _stored_output()
        account = output.account
        performance = pd.DataFrame(
            [
                {
                    "account_id": account.account_id,
                    "currency": account.currency,
                    "initial_cash": account.initial_cash,
                    "final_equity": account.final_equity,
                    "net_pnl": account.net_pnl,
                    **asdict(account.metrics),
                }
            ]
        )
        with (
            patch("librae.db.timescale_reader.get_run", return_value=output.run_metadata),
            patch("librae.db.timescale_reader.load_performance", return_value=performance),
            patch(
                "librae.db.timescale_reader.load_equity_curve",
                return_value=_frame(output.equity_curve),
            ),
            patch(
                "librae.db.timescale_reader.load_position_events",
                return_value=_frame(output.position_events),
            ),
            patch(
                "librae.db.timescale_reader.load_financing_cash_flows",
                return_value=pd.DataFrame(),
            ),
        ):
            loaded = load_backtest_output(output.run_metadata.run_id)

        assert loaded.run_metadata == output.run_metadata
        assert loaded.metrics == output.metrics
        assert loaded.equity_curve == output.equity_curve
        assert loaded.position_events == tuple(output.position_events)
        assert loaded.financing_cash_flows == ()

    def test_an_unknown_run_is_refused(self):
        with (
            patch("librae.db.timescale_reader.get_run", return_value=None),
            pytest.raises(ValueError, match="no stored run"),
        ):
            load_backtest_output("missing")
