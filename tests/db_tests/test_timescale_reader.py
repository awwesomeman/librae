"""Tests for backtest_runs single-row metadata lookups in timescale_reader."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from librae.backtest.schema import StrategyMetrics
from librae.db.timescale_reader import get_run, row_to_strategy_metrics


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
        mock_cur.fetchone.return_value = (
            "demo-20260729t1200-abcdef",
            '{"fast": 10}',
            '{"slippage_bps": 5}',
            None,
        )
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        result = get_run("demo-20260729t1200-abcdef")

        assert result == {
            "run_id": "demo-20260729t1200-abcdef",
            "params": {"fast": 10},
            "execution_policy": {"slippage_bps": 5},
            "risk_policy": None,
        }
        sql = mock_cur.execute.call_args[0][0]
        assert "WHERE run_id = %s" in sql

    @patch("librae.db.timescale_reader.get_conn")
    def test_returns_none_when_not_found(self, mock_conn_ctx):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None
        mock_conn_ctx.return_value = _mock_conn(mock_cur)

        assert get_run("missing-run") is None


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
