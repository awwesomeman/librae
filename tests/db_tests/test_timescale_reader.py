"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
from librae.backtest.schema import RunMetadata, StrategyMetrics
from librae.db.timescale_reader import get_run, load_ohlcv, row_to_strategy_metrics


def _mock_conn(mock_cur: MagicMock) -> MagicMock:
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    return mock_conn


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
        )
        sql = mock_cur.execute.call_args[0][0]
        assert "WHERE run_id = %s" in sql

    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_none_when_not_found(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run("missing-run") is None


class TestLoadOhlcvSessionIdentity:
    @patch("librae.db.timescale_reader.pd.read_sql", return_value=pd.DataFrame())
    @patch("librae.db.timescale_reader.get_conn")
    def test_direct_read_filters_requested_session(self, mock_conn_ctx, mock_read_sql):
        mock_conn_ctx.return_value = _mock_conn(MagicMock())

        load_ohlcv(
            symbol="AAPL",
            timeframe="H1",
            data_source="ibkr",
            session_mode="regular",
        )

        sql = mock_read_sql.call_args.args[0]
        params = mock_read_sql.call_args.kwargs["params"]
        assert "session_mode = %s" in sql
        assert params[-1] == "regular"

    @patch("librae.db.timescale_reader.pd.read_sql", return_value=pd.DataFrame())
    @patch("librae.db.timescale_reader.get_conn")
    def test_run_read_uses_persisted_source_and_session(self, mock_conn_ctx, mock_read_sql):
        mock_conn_ctx.return_value = _mock_conn(MagicMock())

        load_ohlcv(run_id="run-1")

        sql = mock_read_sql.call_args.args[0]
        assert "o.data_source = m.data_source" in sql
        assert "o.session_mode = m.session_mode" in sql


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
