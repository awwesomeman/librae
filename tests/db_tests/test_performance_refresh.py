from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from db.timescale_writer import refresh_performance
from librae import Backtest, Context, CostModel, OrderIntent, Strategy
from librae.backtest.schema import StrategyMetrics
from librae.core.run_config import AccountConfig, RunConfig


def _config(
    *,
    account_id: str = "alpha",
    initial_cash: float = 100.0,
) -> RunConfig:
    return RunConfig(
        strategy_name="refresh_test",
        symbols=["TEST"],
        timeframe="H1",
        market="test",
        data_source="test",
        account=AccountConfig(
            account_id=account_id,
            currency="USD",
            initial_cash=initial_cash,
        ),
        mode="backtest",
    )


def test_refresh_performance_matches_in_memory_backtest_metrics() -> None:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=5, freq="h")
    prices = [100.0, 102.0, 104.0, 106.0, 108.0]
    index = pd.MultiIndex.from_arrays(
        [["TEST"] * len(timestamps), timestamps],
        names=["symbol", "datetime"],
    )
    data = pd.DataFrame(
        {
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1_000.0] * len(timestamps),
        },
        index=index,
    )

    class BuyOnce(Strategy):
        def on_bar(self, ctx: Context):
            if ctx.period_index == 0:
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=2.0)]
            return []

    backtest = Backtest(
        data,
        BuyOnce(),
        initial_balance=1_000.0,
        cost_model=CostModel(
            multiplier=1.0,
            commission_rate=0.001,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.1,
            tax_rate=0.001,
        ),
        data_source="test",
    )
    backtest.run()
    output = backtest.build_output()

    equity = pd.DataFrame([{**asdict(point), "_time": point.ts} for point in output.equity_curve])
    closed = pd.DataFrame(
        [asdict(event) for event in output.order_events if event.event_type in {"close", "reduce"}]
    )
    config = _config(account_id="default", initial_cash=1_000.0)

    with (
        patch("db.timescale_reader.load_equity_curve", return_value=equity),
        patch("db.timescale_reader.load_trade_events", return_value=closed),
        patch("db.timescale_writer.write_strategy_performance") as write,
    ):
        refresh_performance(output.run_metadata.run_id, "default", config=config)

    refreshed = write.call_args.args[6]
    expected = asdict(output.metrics)
    actual = asdict(refreshed)
    assert actual.keys() == expected.keys()
    for field, expected_value in expected.items():
        if expected_value is None:
            assert actual[field] is None
        else:
            assert actual[field] == pytest.approx(expected_value)


def test_refresh_performance_reconstructs_persisted_quant_inputs() -> None:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=3, freq="h")
    equity = pd.DataFrame(
        {
            "_time": timestamps,
            "equity": [100.0, 110.0, 120.0],
            "gross_exposure": [0.0, 0.5, 0.0],
            "net_exposure": [0.0, 0.5, 0.0],
            "concentration": [0.0, 0.5, 0.0],
            "turnover": [0.0, 0.5, 0.5],
            # Final forced liquidation leaves end-of-bar gross exposure at
            # zero, while the account was still exposed during that bar.
            "exposed": [False, True, True],
        }
    )
    closed = pd.DataFrame(
        [
            {
                "pnl": 7.0,
                "commission": 0.7,
                "slippage": 0.8,
                "tax": 1.0,
                "entry_commission": 1.0,
                "entry_slippage": 2.0,
                "entry_tax": 0.5,
                "net_return": 3.5,
                "fill_quantity": 2.0,
                "price": 110.0,
                "entry_price": 100.0,
                "notional": 220.0,
            }
        ]
    )
    metrics = StrategyMetrics(total_return=0.2)

    with (
        patch("db.timescale_reader.load_equity_curve", return_value=equity),
        patch("db.timescale_reader.load_trade_events", return_value=closed),
        patch("librae.core.metrics.compute_all", return_value=metrics) as compute,
        patch("db.timescale_writer.write_strategy_performance") as write,
    ):
        refresh_performance("run-1", "alpha", config=_config())

    kwargs = compute.call_args.kwargs
    trade = kwargs["trade_pnls"][0]
    assert trade.gross_pnl == pytest.approx(13.0)
    assert trade.net_pnl == pytest.approx(7.0)
    assert trade.commission == pytest.approx(1.7)
    assert trade.slippage == pytest.approx(2.8)
    assert trade.tax == pytest.approx(1.5)
    assert kwargs["trade_notionals"] == pytest.approx([200.0])
    assert kwargs["exposed_periods"] == 2
    assert kwargs["turnover_values"] == pytest.approx([0.0, 0.5, 0.5])
    write.assert_called_once()


def test_refresh_performance_rejects_legacy_close_without_entry_costs() -> None:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=2, freq="h")
    equity = pd.DataFrame(
        {
            "_time": timestamps,
            "equity": [100.0, 101.0],
            "gross_exposure": [None, None],
            "net_exposure": [None, None],
            "concentration": [None, None],
            "turnover": [None, None],
            "exposed": [None, None],
        }
    )
    closed = pd.DataFrame(
        [
            {
                "pnl": 1.0,
                "commission": 0.1,
                "slippage": 0.0,
                "tax": 0.0,
                "entry_commission": None,
                "entry_slippage": None,
                "entry_tax": None,
                "net_return": 1.0,
                "fill_quantity": 1.0,
                "price": 101.0,
                "entry_price": 100.0,
                "notional": 101.0,
            }
        ]
    )

    with (
        patch("db.timescale_reader.load_equity_curve", return_value=equity),
        patch("db.timescale_reader.load_trade_events", return_value=closed),
        pytest.raises(ValueError, match="entry_commission"),
    ):
        refresh_performance("run-1", "alpha", config=_config())
