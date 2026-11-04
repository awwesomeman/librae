"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
from librae.backtest.schema import RunMetadata, StrategyMetrics
from librae.core.market_data import MarketDataSubscription
from librae.db.timescale_reader import get_run, load_run_bars, row_to_strategy_metrics


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
            '["BTCUSDT"]',
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
        """The table keeps each version of a bar; a chart wants one per bar."""
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
        assert "ORDER BY symbol, ts, available_at DESC" in sql
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
