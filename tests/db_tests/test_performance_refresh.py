from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import patch

import pandas as pd
import pytest
from librae import Backtest, Context, CostModel, OrderIntent, Strategy
from librae.backtest.schema import StrategyMetrics
from librae.core.run_config import AccountConfig, RunConfig
from librae.db.timescale_writer import refresh_performance

from tests.conftest import declare_instruments


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
        instrument_overrides=declare_instruments(data),
    )
    backtest.run()
    output = backtest.build_output()

    equity = pd.DataFrame([{**asdict(point), "_time": point.ts} for point in output.equity_curve])
    closed = pd.DataFrame(
        [
            asdict(event)
            for event in output.position_events
            if event.event_type in {"close", "reduce"}
        ]
    )
    config = _config(account_id="default", initial_cash=1_000.0)

    with (
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=closed),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=pd.DataFrame()),
        patch("librae.db.timescale_writer.write_strategy_performance") as write,
    ):
        refresh_performance(output.run_metadata.run_id, "default", config=config)

    _assert_same_metrics(write.call_args.args[6], output.metrics)


def _assert_same_metrics(actual: StrategyMetrics, expected: StrategyMetrics) -> None:
    actual_fields, expected_fields = asdict(actual), asdict(expected)
    assert actual_fields.keys() == expected_fields.keys()
    for field, expected_value in expected_fields.items():
        if expected_value is None:
            assert actual_fields[field] is None, field
        else:
            assert actual_fields[field] == pytest.approx(expected_value), field


class _HedgeThenSingle(Strategy):
    """A two-perp hedge that is partly reduced and then closed, followed by an
    ungrouped position the end of the run liquidates."""

    def on_bar(self, ctx: Context):
        if ctx.period_index == 0:
            return [
                OrderIntent(action="long", symbol="A", quantity=4.0, group_id="hedge"),
                OrderIntent(action="short", symbol="B", quantity=4.0, group_id="hedge"),
            ]
        if ctx.period_index == 2:
            return [
                OrderIntent(action="close", symbol="A", quantity=1.0),
                OrderIntent(action="close", symbol="B", quantity=1.0),
            ]
        if ctx.period_index == 4:
            return [
                OrderIntent(action="close", symbol="A"),
                OrderIntent(action="close", symbol="B"),
            ]
        if ctx.period_index == 5:
            return [OrderIntent(action="long", symbol="A", quantity=2.0)]
        return []


def test_backtest_refresh_and_closed_trades_agree_on_a_funded_partly_reduced_hedge() -> None:
    """The backtest summary, a refresh from the stored rows, and closed_trades
    must see the same trades: the hedge's two legs and both of their exits
    are one trade, funding follows the quantity it accrued on, and the final
    liquidation takes the last bar's funding."""
    from librae.core.metrics import closed_trades, compute_all

    from tests.conftest import primary_subscriptions_for

    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=8, freq="h")
    funded = [i % 2 == 1 for i in range(len(timestamps))]
    frames = []
    for symbol, prices in {
        "A": [100.0, 101.0, 103.0, 102.0, 105.0, 104.0, 106.0, 108.0],
        "B": [50.0, 50.5, 51.0, 51.5, 52.5, 52.0, 53.0, 54.0],
    }.items():
        frames.append(
            pd.DataFrame(
                {
                    "open": prices,
                    "high": prices,
                    "low": prices,
                    "close": prices,
                    "volume": 1_000.0,
                    "funding_rate": [0.001 if paid else float("nan") for paid in funded],
                    "funding_settled_at": timestamps.where(funded),
                },
                index=pd.MultiIndex.from_arrays(
                    [[symbol] * len(timestamps), timestamps], names=["symbol", "datetime"]
                ),
            )
        )
    data = pd.concat(frames).sort_index(level="datetime", sort_remaining=False)
    backtest = Backtest(
        data,
        _HedgeThenSingle(),
        initial_balance=10_000.0,
        cost_model=CostModel(
            multiplier=10.0,
            commission_rate=0.0005,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            long_margin_rate=0.1,
            short_margin_rate=0.1,
            long_margin_mode="dynamic",
            short_margin_mode="dynamic",
        ),
        data_source="test",
        primary_subscriptions=primary_subscriptions_for(
            data, "H1", instrument_type="contract_perpetual"
        ),
    )
    backtest.run()
    with patch("librae.core.metrics.compute_all", wraps=compute_all) as in_memory:
        output = backtest.build_output()
    backtest_trades = in_memory.call_args.kwargs["trades"]

    # What the readers return: every event and payment, timestamps as ``_time``.
    stored_events = pd.DataFrame(output.position_events).rename(columns={"ts": "_time"})
    stored_funding = pd.DataFrame(output.financing_cash_flows).rename(columns={"ts": "_time"})
    equity = pd.DataFrame([{**asdict(point), "_time": point.ts} for point in output.equity_curve])
    with (
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=stored_events),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=stored_funding),
        patch("librae.core.metrics.compute_all", wraps=compute_all) as refreshed,
        patch("librae.db.timescale_writer.write_strategy_performance") as write,
    ):
        refresh_performance(
            output.run_metadata.run_id,
            "default",
            config=_config(account_id="default", initial_cash=10_000.0),
        )
    refresh_trades = refreshed.call_args.kwargs["trades"]

    trades = closed_trades(
        pd.DataFrame(output.position_events), pd.DataFrame(output.financing_cash_flows)
    )
    assert "reduce" in {event.event_type for event in output.position_events}
    assert [row.event_type for row in output.position_events][-1] == "close"
    assert output.position_events[-1].reason == "force_close"
    assert trades["symbol"].tolist() == ["A / B", "A"]
    assert trades["group_id"].tolist() == ["hedge", None]
    assert (trades["financing"].abs() > 0).all()
    # The hedge's exits release all of its funding; the single trade's is its own.
    assert trades["financing"].sum() == pytest.approx(
        sum(flow.cash_flow for flow in output.financing_cash_flows)
    )
    pd.testing.assert_frame_equal(backtest_trades, trades)
    pd.testing.assert_frame_equal(refresh_trades, trades)
    assert output.metrics.trades == 2
    _assert_same_metrics(write.call_args.args[6], output.metrics)


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
                "_time": timestamps[2],
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "event_type": "close",
                "reason": "",
                "group_id": None,
                "periods_held": 2,
                "realized_pnl": 7.0,
                "commission": 0.7,
                "slippage": 0.8,
                "tax": 1.0,
                "entry_commission": 1.0,
                "entry_slippage": 2.0,
                "entry_tax": 0.5,
                "net_return": 3.5,
                "fill_quantity": 2.0,
                "remaining_quantity": 0.0,
                "price": 110.0,
                "entry_price": 100.0,
                "notional": 220.0,
                "entry_at": timestamps[0],
            }
        ]
    )
    funding = pd.DataFrame(
        [
            {
                "_time": timestamps[1],
                "symbol": "BTC/USDT:USDT",
                "kind": "funding",
                "side": "long",
                "quantity": 2.0,
                "mark_price": 100.0,
                "multiplier": 1.0,
                "rate": -0.015,
                "cash_flow": 3.0,
                "group_id": None,
                "entry_at": timestamps[0],
            }
        ]
    )
    metrics = StrategyMetrics(total_return=0.2)

    with (
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=closed),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=funding),
        patch("librae.core.metrics.compute_all", return_value=metrics) as compute,
        patch("librae.db.timescale_writer.write_strategy_performance") as write,
    ):
        refresh_performance("run-1", "alpha", config=_config())

    kwargs = compute.call_args.kwargs
    trade = kwargs["trades"].iloc[0]
    # Funding accrued for this round-trip (3.0) is folded into net PnL and
    # return on top of the fill-only figure (7.0).
    assert trade["net_pnl"] == pytest.approx(10.0)
    assert trade["financing"] == pytest.approx(3.0)
    assert trade["net_return"] == pytest.approx(0.05)  # 10.0 / 200.0
    assert trade["commission"] == pytest.approx(1.7)
    assert trade["slippage"] == pytest.approx(2.8)
    assert trade["tax"] == pytest.approx(1.5)
    assert trade["entry_notional"] == pytest.approx(200.0)
    assert kwargs["exposed_periods"] == 2
    assert kwargs["turnover_values"] == pytest.approx([0.0, 0.5, 0.5])
    write.assert_called_once()


def test_refresh_performance_attributes_funding_by_close_time() -> None:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=5, freq="h")
    equity = pd.DataFrame({"_time": timestamps, "equity": [100.0, 102.0, 104.0, 108.0, 110.0]})
    entry_at = timestamps[0]

    def _row(
        ts: datetime,
        event_type: str,
        fill_quantity: float,
        remaining_quantity: float,
    ) -> dict:
        return {
            "_time": ts,
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "event_type": event_type,
            "reason": "",
            "group_id": None,
            "periods_held": 2,
            "realized_pnl": 0.0,
            "commission": 0.0,
            "slippage": 0.0,
            "tax": 0.0,
            "entry_commission": 0.0,
            "entry_slippage": 0.0,
            "entry_tax": 0.0,
            "net_return": 0.0,
            "fill_quantity": fill_quantity,
            "remaining_quantity": remaining_quantity,
            "price": 100.0,
            "entry_price": 100.0,
            "notional": fill_quantity * 100.0,
            "entry_at": entry_at,
        }

    closed = pd.DataFrame(
        [
            _row(timestamps[2], "reduce", 3.0, 2.0),
            _row(timestamps[4], "close", 2.0, 0.0),
        ]
    )
    funding = pd.DataFrame(
        [
            {
                "_time": timestamps[1],
                "symbol": "BTC/USDT:USDT",
                "kind": "funding",
                "side": "short",
                "quantity": 5.0,
                "mark_price": 100.0,
                "multiplier": 1.0,
                "rate": 0.01,
                "cash_flow": 50.0,
                "group_id": None,
                "entry_at": entry_at,
            },
            {
                "_time": timestamps[3],
                "symbol": "BTC/USDT:USDT",
                "kind": "funding",
                "side": "short",
                "quantity": 2.0,
                "mark_price": 100.0,
                "multiplier": 1.0,
                "rate": 0.01,
                "cash_flow": 20.0,
                "group_id": None,
                "entry_at": entry_at,
            },
        ]
    )
    metrics = StrategyMetrics(total_return=0.2)

    with (
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=closed),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=funding),
        patch("librae.core.metrics.compute_all", return_value=metrics) as compute,
        patch("librae.db.timescale_writer.write_strategy_performance"),
    ):
        refresh_performance("run-1", "alpha", config=_config())

    trades = compute.call_args.kwargs["trades"]
    # The later 20 belongs only to the two units still open after the first exit.
    assert trades["net_pnl"].tolist() == pytest.approx([30.0, 40.0])


def test_refresh_performance_keeps_the_last_bar_financing_on_the_forced_close() -> None:
    """The stored ``reason`` is what tells the replay that this close ran after
    the last bar's accrual, exactly as the in-memory engine ordered it — the
    two must agree or a run that attributes in memory fails once persisted."""
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=3, freq="h")
    equity = pd.DataFrame({"_time": timestamps, "equity": [100.0, 100.0, 120.0]})
    entry_at = timestamps[0]
    closed = pd.DataFrame(
        [
            {
                "_time": timestamps[2],
                "symbol": "BTC/USDT:USDT",
                "side": "short",
                "event_type": "close",
                "reason": "force_close",
                "group_id": None,
                "periods_held": 2,
                "realized_pnl": 0.0,
                "commission": 0.0,
                "slippage": 0.0,
                "tax": 0.0,
                "entry_commission": 0.0,
                "entry_slippage": 0.0,
                "entry_tax": 0.0,
                "net_return": 0.0,
                "fill_quantity": 2.0,
                "remaining_quantity": 0.0,
                "price": 100.0,
                "entry_price": 100.0,
                "notional": 200.0,
                "entry_at": entry_at,
            }
        ]
    )
    funding = pd.DataFrame(
        [
            {
                "_time": timestamps[2],
                "symbol": "BTC/USDT:USDT",
                "kind": "funding",
                "side": "short",
                "quantity": 2.0,
                "mark_price": 100.0,
                "multiplier": 1.0,
                "rate": 0.01,
                "cash_flow": 20.0,
                "group_id": None,
                "entry_at": entry_at,
            }
        ]
    )
    metrics = StrategyMetrics(total_return=0.2)

    with (
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=closed),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=funding),
        patch("librae.core.metrics.compute_all", return_value=metrics) as compute,
        patch("librae.db.timescale_writer.write_strategy_performance"),
    ):
        refresh_performance("run-1", "alpha", config=_config())

    assert compute.call_args.kwargs["trades"]["net_pnl"].iloc[0] == pytest.approx(20.0)


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
                "_time": timestamps[1],
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "event_type": "close",
                "reason": "",
                "group_id": None,
                "periods_held": 1,
                "entry_at": timestamps[0],
                "remaining_quantity": 0.0,
                "realized_pnl": 1.0,
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
        patch("librae.db.timescale_reader.load_equity_curve", return_value=equity),
        patch("librae.db.timescale_reader.load_position_events", return_value=closed),
        patch("librae.db.timescale_reader.load_financing_cash_flows", return_value=pd.DataFrame()),
        pytest.raises(ValueError, match="entry_commission"),
    ):
        refresh_performance("run-1", "alpha", config=_config())


def test_refresh_performance_bounds_only_its_upsert() -> None:
    """The live loop waits on it; its reads cover the whole run and may run long."""
    import librae.db as db

    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=2, freq="h")
    equity = pd.DataFrame({"_time": timestamps, "equity": [100.0, 101.0]})
    bounded: dict[str, bool] = {}

    def recorded(name: str, result):
        def call(*_args, **_kwargs):
            bounded[name] = db._bounded.get()
            return result

        return call

    with (
        patch("librae.db.timescale_reader.load_equity_curve", recorded("equity", equity)),
        patch(
            "librae.db.timescale_reader.load_position_events", recorded("events", pd.DataFrame())
        ),
        patch(
            "librae.db.timescale_reader.load_financing_cash_flows",
            recorded("funding", pd.DataFrame()),
        ),
        patch("librae.core.metrics.compute_all", return_value=StrategyMetrics(total_return=0.01)),
        patch("librae.db.timescale_writer.write_strategy_performance", recorded("upsert", None)),
    ):
        refresh_performance("run-1", "alpha", config=_config())

    assert bounded == {"equity": False, "events": False, "funding": False, "upsert": True}
