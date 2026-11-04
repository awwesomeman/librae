"""Tests for the TimescaleDB trade-chart adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from librae.backtest.charts import _build_markers
from librae.db.charts import df_to_position_events, plot_trades_by_run_id


def test_df_to_position_events_matches_reader_shape() -> None:
    df = pd.DataFrame(
        [
            {
                "event_id": "e1",
                "_time": datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
                "account_id": "default",
                "currency": "USDT",
                "symbol": "BTCUSDT",
                "side": "long",
                "event_type": "open",
                "fill_quantity": 1.0,
                "price": 50_000.0,
                "entry_price": 50_000.0,
                "remaining_quantity": 1.0,
                "notional": 50_000.0,
                "commission": 1.0,
                "slippage": 0.0,
                "tax": 0.0,
                "realized_pnl": None,
                "net_return": None,
                "entry_at": None,
                "periods_held": None,
                "reason": "",
            }
        ]
    )

    events = df_to_position_events(df)

    assert len(events) == 1
    assert events[0].symbol == "BTCUSDT"
    assert events[0].ts == datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
    markers = _build_markers(events, "BTCUSDT")
    assert len(markers) == 1
    assert markers[0]["shape"] == "arrow_up"


@pytest.mark.parametrize(
    "bars",
    [
        pd.DataFrame(),
        pd.DataFrame(columns=["_time", "symbol", "open", "high", "low", "close", "volume"]),
    ],
)
def test_plot_trades_by_run_id_returns_none_when_run_stored_no_bars(
    bars: pd.DataFrame,
) -> None:
    """A backtest saved without store_bars=True has nothing to draw."""
    with (
        patch("librae.db.charts.load_run_bars", return_value=bars),
        patch("librae.db.charts.load_position_events") as load_events,
        patch("librae.db.charts.plot_kbars") as render,
    ):
        result = plot_trades_by_run_id("empty-run", block=False)

    assert result is None
    load_events.assert_not_called()
    render.assert_not_called()


def test_plot_trades_by_run_id_propagates_reader_errors() -> None:
    with (
        patch(
            "librae.db.charts.load_run_bars",
            side_effect=RuntimeError("database unavailable"),
        ),
        pytest.raises(RuntimeError, match="database unavailable"),
    ):
        plot_trades_by_run_id("failed-run", block=False)


def test_plot_trades_by_run_id_draws_only_the_chosen_symbols_bars() -> None:
    """The renderer takes one symbol's candles; a portfolio run stores several."""
    times = pd.to_datetime(["2026-03-01T10:00Z", "2026-03-01T11:00Z"], utc=True)
    bars = pd.DataFrame(
        {
            "_time": list(times) * 2,
            "symbol": ["BTCUSDT", "BTCUSDT", "ETHUSDT", "ETHUSDT"],
            "open": [1.0, 2.0, 10.0, 20.0],
            "high": [1.0, 2.0, 10.0, 20.0],
            "low": [1.0, 2.0, 10.0, 20.0],
            "close": [1.0, 2.0, 10.0, 20.0],
            "volume": [1.0, 1.0, 1.0, 1.0],
        }
    )
    with (
        patch("librae.db.charts.load_run_bars", return_value=bars),
        patch("librae.db.charts.load_position_events", return_value=pd.DataFrame()),
        patch("librae.db.charts.plot_kbars") as render,
    ):
        plot_trades_by_run_id("portfolio-run", symbol="ETHUSDT", block=False)

    drawn, _events, symbol = render.call_args.args
    assert symbol == "ETHUSDT"
    assert drawn["close"].tolist() == [10.0, 20.0]
    assert list(drawn.index) == list(times)


def test_plot_trades_by_run_id_returns_none_for_a_symbol_with_no_stored_bars() -> None:
    bars = pd.DataFrame(
        {
            "_time": pd.to_datetime(["2026-03-01T10:00Z"], utc=True),
            "symbol": ["BTCUSDT"],
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [1.0],
        }
    )
    with (
        patch("librae.db.charts.load_run_bars", return_value=bars),
        patch("librae.db.charts.load_position_events", return_value=pd.DataFrame()),
        patch("librae.db.charts.plot_kbars") as render,
    ):
        assert plot_trades_by_run_id("run", symbol="ETHUSDT", block=False) is None

    render.assert_not_called()
