"""Tests for signal_outcomes write path in timescale_writer."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from db.timescale_writer import persist_backtest, write_signal_outcome


class TestWriteSignalOutcome:
    """write_signal_outcome single-row upsert."""

    @patch("db.timescale_writer.get_conn")
    def test_inserts_row(self, mock_conn_ctx):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        mock_conn_ctx.return_value = mock_conn

        ts = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        write_signal_outcome(
            signal_ts=ts, strategy="test_strat", symbol="BTCUSDT",
            source="sim", timeframe="H1", signal_value=1.0, price=50000.0,
        )

        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "signal_outcomes" in sql
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql

    @patch("db.timescale_writer.get_conn")
    def test_accepts_cursor(self, mock_conn_ctx):
        """When cur is provided, uses it directly without opening connection."""
        mock_cur = MagicMock()
        ts = datetime(2024, 6, 1, tzinfo=timezone.utc)

        write_signal_outcome(
            signal_ts=ts, strategy="s", symbol="S", source="sim",
            timeframe="H1", signal_value=1.0, cur=mock_cur,
        )

        mock_cur.execute.assert_called_once()
        mock_conn_ctx.assert_not_called()


class TestPersistBacktest:
    """persist_backtest extracts signals and writes to DB."""

    def _make_featured_df(self, n: int = 20) -> tuple[pd.DataFrame, str]:
        """Create a MultiIndex DataFrame mimicking strategy output."""
        symbol = "BTCUSDT"
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        mi = pd.MultiIndex.from_arrays(
            [[symbol] * n, idx], names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": range(n),
                "high": [x + 1 for x in range(n)],
                "low": [max(0, x - 1) for x in range(n)],
                "close": [x + 0.5 for x in range(n)],
                "volume": [100.0] * n,
                "entry_signal": [True if i % 5 == 0 else False for i in range(n)],
            },
            index=mi,
        )
        return df, symbol

    @patch("db.timescale_writer.write_ohlcv", return_value=20)
    @patch("db.timescale_writer.write_backtest_output", return_value={"backtest_runs": 1})
    def test_extracts_signals_and_calls_writer(self, mock_write_bt, mock_write_ohlcv):
        df, symbol = self._make_featured_df()
        mock_output = MagicMock()

        counts = persist_backtest(mock_output, df, symbol, "H1", params={"a": 1})

        mock_write_bt.assert_called_once()
        call_kwargs = mock_write_bt.call_args
        signal_series = call_kwargs.kwargs.get("signal_series")
        if signal_series is None:
            signal_series = call_kwargs[1].get("signal_series")
        assert signal_series is not None
        # entry_signal is True every 5th bar → 4 signals (indices 0,5,10,15)
        assert len(signal_series) == 4
        assert all(v == 1.0 for v in signal_series.values.tolist())

        assert counts["ohlcv"] == 20

    @patch("db.timescale_writer.write_ohlcv", return_value=20)
    @patch("db.timescale_writer.write_backtest_output", return_value={})
    def test_excludes_nan_and_zero_signals(self, mock_write_bt, mock_write_ohlcv):
        """NaN and 0 values are excluded from signal_series."""
        symbol = "BTCUSDT"
        n = 10
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        mi = pd.MultiIndex.from_arrays([[symbol] * n, idx], names=["symbol", "datetime"])
        signals = [1.0, 0.0, float("nan"), -1.0, 0.0, 1.0, float("nan"), 0.0, -0.5, 1.0]
        df = pd.DataFrame({
            "open": range(n), "high": range(n), "low": range(n),
            "close": range(n), "volume": [100] * n,
            "entry_signal": signals,
        }, index=mi)

        persist_backtest(MagicMock(), df, symbol, "H1")

        signal_series = mock_write_bt.call_args.kwargs["signal_series"]
        # Should keep: 1.0, -1.0, 1.0, -0.5, 1.0 (5 values, excluding NaN and 0)
        assert len(signal_series) == 5
        assert 0.0 not in signal_series.values
