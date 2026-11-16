"""Perpetual-funding and short-borrow cash-flow accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, Context, CostModel, OrderIntent, Strategy
from librae.backtest.engine import _attribute_financing_to_trades
from librae.core.executor import PositionEvent, TradeResult
from librae.core.financing import (
    BORROW_RATE_MAX_AGE_QUOTING_PERIODS,
    FinancingCashFlow,
    FinancingLifecycleEvent,
    attach_borrow_rate,
    attach_funding_rate,
    attribute_financing_to_closes,
    calculate_borrow_cash_flows,
    calculate_funding_cash_flows,
)
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import PositionState
from librae.core.utils import interval_to_timedelta
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore

from tests.conftest import make_test_cfg, primary_subscriptions_for


def _cost_model(multiplier: float = 10.0) -> CostModel:
    return CostModel(
        multiplier=multiplier,
        commission_rate=0.0,
        min_commission=0.0,
        slippage_ticks=0.0,
        tick_size=0.01,
        tax_rate=0.0,
        long_margin_rate=0.1,
        short_margin_rate=0.1,
        long_margin_mode="dynamic",
        short_margin_mode="dynamic",
    )


def _position(side: str = "long") -> PositionState:
    return PositionState(
        symbol="PERP",
        side=side,
        entry_price=100.0,
        quantity=2.0,
        entry_at=datetime(2026, 1, 1, tzinfo=UTC),
        periods_held=1,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=200.0,
    )


def _backtest_frame(
    funding_rates: list[float],
    *,
    mark_prices: list[float] | None = None,
    rate_field: str = "funding_rate",
) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=len(funding_rates), freq="h", tz="UTC")
    index = pd.MultiIndex.from_arrays(
        [["PERP"] * len(timestamps), timestamps],
        names=["symbol", "datetime"],
    )
    data = pd.DataFrame(
        {
            "open": 100.0,
            "high": 100.0,
            "low": 100.0,
            "close": 100.0,
            "volume": 1_000.0,
            rate_field: funding_rates,
        },
        index=index,
    )
    if rate_field == "funding_rate":
        data["funding_settled_at"] = timestamps.where(data["funding_rate"].notna().to_numpy())
    if mark_prices is not None:
        data["funding_mark_price"] = mark_prices
    return data


def _subscriptions(data: pd.DataFrame):
    """A frame carrying funding is a perpetual's; any other one is spot."""
    instrument_type = "contract_perpetual" if "funding_rate" in data.columns else "spot"
    return primary_subscriptions_for(data, "H1", instrument_type=instrument_type)


class _OpenOnce(Strategy):
    def __init__(self, side: str = "long") -> None:
        self.side = side

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index == 0:
            return [OrderIntent(action=self.side, symbol=ctx.symbol, quantity=2.0)]
        return []


class _OpenThenClose(Strategy):
    """Opens on bar 0, signals close one bar before the end (so the close
    fills normally on the last bar instead of via end-of-run forced
    liquidation) — trade-level stats then see a single closed round-trip
    whose only PnL source is funding accrued while held (flat price + zero
    costs leave basis convergence at 0)."""

    def __init__(self, side: str, n_bars: int) -> None:
        self.side = side
        self._n_bars = n_bars

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index == 0:
            return [OrderIntent(action=self.side, symbol=ctx.symbol, quantity=2.0)]
        if ctx.period_index == self._n_bars - 2:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return []


class _OpenThenPartialClose(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index == 0:
            return [OrderIntent(action="long", symbol=ctx.symbol, quantity=4.0)]
        if ctx.period_index == 1:
            return [OrderIntent(action="close", symbol=ctx.symbol, quantity=2.0)]
        if ctx.period_index == 3:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return []


@pytest.mark.parametrize(
    ("side", "expected_cash_flow"),
    [("long", -20.0), ("short", 20.0)],
)
def test_positive_rate_means_longs_pay_shorts(side: str, expected_cash_flow: float) -> None:
    observed, cash_flows = calculate_funding_cash_flows(
        datetime(2026, 1, 1, tzinfo=UTC),
        {"PERP": {"close": 100.0, "funding_rate": 0.01}},
        {"PERP": _position(side)},
        get_cost_model=lambda _symbol: _cost_model(),
    )

    assert observed == ("PERP",)
    assert len(cash_flows) == 1
    assert cash_flows[0].cash_flow == pytest.approx(expected_cash_flow)


def test_missing_rate_and_flat_position_do_not_create_payments() -> None:
    ts = datetime(2026, 1, 1, tzinfo=UTC)

    observed, cash_flows = calculate_funding_cash_flows(
        ts,
        {"PERP": {"close": 100.0, "funding_rate": np.nan}},
        {"PERP": _position()},
        get_cost_model=lambda _symbol: _cost_model(),
    )
    assert observed == ()
    assert cash_flows == []

    observed, cash_flows = calculate_funding_cash_flows(
        ts,
        {"PERP": {"close": 100.0, "funding_rate": 0.01}},
        {},
        get_cost_model=lambda _symbol: _cost_model(),
    )
    assert observed == ("PERP",)
    assert cash_flows == []


@pytest.mark.parametrize(
    "bar",
    [
        {"close": 100.0, "funding_rate": float("inf")},
        {"close": 100.0, "funding_rate": True},
        {"close": 0.0, "funding_rate": 0.01},
        {"close": 100.0, "funding_rate": 0.01, "funding_mark_price": -1.0},
    ],
)
def test_invalid_funding_inputs_fail_closed(bar: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=r"funding|financing"):
        calculate_funding_cash_flows(
            datetime(2026, 1, 1, tzinfo=UTC),
            {"PERP": bar},
            {"PERP": _position()},
            get_cost_model=lambda _symbol: _cost_model(),
        )


def test_backtest_applies_only_same_timestamp_observations() -> None:
    data = _backtest_frame([0.5, 0.01, np.nan, -0.005, np.nan])
    backtest = Backtest(
        data,
        _OpenOnce(),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )

    result = backtest.run()
    output = backtest.build_output()

    assert [item.cash_flow for item in result.financing_cash_flows] == pytest.approx([-20.0, 10.0])
    assert result.final_equity == pytest.approx(9_990.0)
    assert output.account.net_pnl == pytest.approx(-10.0)
    assert output.metrics.total_return == pytest.approx(-0.001)
    assert [item.cash_flow for item in output.financing_cash_flows] == pytest.approx([-20.0, 10.0])
    assert all(event.price == 100.0 for event in output.position_events)


def test_backtest_uses_explicit_funding_mark_price_and_multiplier() -> None:
    data = _backtest_frame(
        [np.nan, 0.01, np.nan, np.nan, np.nan],
        mark_prices=[np.nan, 110.0, np.nan, np.nan, np.nan],
    )
    result = Backtest(
        data,
        _OpenOnce(side="short"),
        initial_balance=10_000.0,
        cost_model=_cost_model(multiplier=5.0),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    ).run()

    assert len(result.financing_cash_flows) == 1
    assert result.financing_cash_flows[0].cash_flow == pytest.approx(11.0)
    assert result.final_equity == pytest.approx(10_011.0)


def test_financing_cash_flow_carries_the_accruing_position_group_id_and_entry_at() -> None:
    position = _position()
    observed, cash_flows = calculate_funding_cash_flows(
        datetime(2026, 1, 1, 1, tzinfo=UTC),
        {"PERP": {"close": 100.0, "funding_rate": 0.01}},
        {"PERP": position},
        get_cost_model=lambda _symbol: _cost_model(),
    )

    assert observed == ("PERP",)
    assert cash_flows[0].group_id == position.group_id
    assert cash_flows[0].entry_at == position.entry_at


def test_funding_accrued_while_held_is_folded_into_the_closed_trade_stats() -> None:
    """Flat price + zero costs mean the only real PnL for this round-trip is
    the funding received while short — win_rate/avg_trade_return must see
    it, not just the (zero) basis-convergence PnL position_events records."""
    data = _backtest_frame([np.nan, 0.01, np.nan, np.nan, np.nan])
    backtest = Backtest(
        data,
        _OpenThenClose(side="short", n_bars=5),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    backtest.run()
    output = backtest.build_output()

    assert [item.cash_flow for item in output.financing_cash_flows] == pytest.approx([20.0])
    assert output.metrics.trades == 1
    assert output.metrics.win_rate == pytest.approx(1.0)
    # notional = entry_price(100) * quantity(2) * multiplier(10) = 2_000;
    # funding(20)/notional*100 = 1.0% — the only PnL, since price is flat
    # and costs are zero.
    assert output.metrics.avg_trade_return == pytest.approx(0.01)


def test_backtest_metrics_assign_later_funding_only_to_remaining_quantity() -> None:
    data = _backtest_frame([np.nan, 0.01, np.nan, 0.01, np.nan])
    backtest = Backtest(
        data,
        _OpenThenPartialClose(),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    backtest.run()

    output = backtest.build_output()

    assert [flow.cash_flow for flow in output.financing_cash_flows] == pytest.approx([-40.0, -20.0])
    assert output.equity_curve[-1].equity == pytest.approx(9_940.0)
    assert output.metrics.trades == 2
    assert output.metrics.avg_trade_return == pytest.approx(-0.015)


@pytest.mark.parametrize(
    ("rate_field", "expected_financing"),
    [("funding_rate", 40.0), ("borrow_rate", -40.0)],
)
def test_financing_on_the_final_bar_reaches_the_end_of_run_forced_close(
    rate_field: str, expected_financing: float
) -> None:
    """A position held into the last bar did live through that bar's
    settlement, so the forced liquidation is attributed after it — every bar
    of a funding-arb run is a settlement, and refusing this blocked the whole
    strategy. Funding and borrow share the accrual pass, so both get here.

    The open bar's accrual pins the other direction: it still follows the fill
    that created the position.
    """
    data = _backtest_frame([np.nan, 0.01, np.nan, np.nan, 0.01], rate_field=rate_field)
    backtest = Backtest(
        data,
        _OpenOnce(side="short"),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    result = backtest.run()
    output = backtest.build_output()

    # 2 units * 100 * multiplier 10 * 1%, on the open bar and the last one.
    assert [flow.cash_flow for flow in result.financing_cash_flows] == pytest.approx(
        [expected_financing / 2] * 2
    )
    assert output.position_events[-1].reason == "force_close"
    assert output.metrics.trades == 1
    trade_pnls = _attribute_financing_to_trades(
        result.trades,
        [abs(trade.entry_price * trade.quantity * 10.0) for trade in result.trades],
        result.position_events,
        result.financing_cash_flows,
    )
    # Flat price and zero costs leave financing as the round-trip's whole PnL.
    assert trade_pnls[0].net_pnl == pytest.approx(expected_financing)


def test_a_liquidity_capped_terminal_close_releases_its_share_of_the_last_accrual() -> None:
    """The last bar's liquidity budget can absorb only part of the position,
    and there is no later bar to retry on, so the end-of-run liquidation
    leaves a residual. That partial close is ordered the same way as a full
    one — after the accrual — and releases its fraction of a balance that
    includes it."""
    data = _backtest_frame([np.nan, 0.01, np.nan, np.nan, 0.01])
    # Default participation cap is 10% of bar volume: 1 of the 2 units held.
    data.loc[data.index[-1], "volume"] = 10.0
    backtest = Backtest(
        data,
        _OpenOnce(side="short"),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    result = backtest.run()
    backtest.build_output()

    terminal_event = result.position_events[-1]
    assert (terminal_event.event_type, terminal_event.reason) == ("reduce", "force_close")
    assert terminal_event.remaining_quantity == pytest.approx(1.0)
    trade_pnls = _attribute_financing_to_trades(
        result.trades,
        [abs(trade.entry_price * trade.quantity * 10.0) for trade in result.trades],
        result.position_events,
        result.financing_cash_flows,
    )
    # Both accruals (20 each) are in the balance when half the position is
    # closed, so the closed half takes 20 and the residual keeps 20.
    assert trade_pnls[0].net_pnl == pytest.approx(20.0)


def test_partial_closes_split_funding_by_closed_quantity_not_double_count_it() -> None:
    """A partial close writes multiple TradeResults sharing one (symbol,
    entry_at). Funding accrued over that round-trip must be split across
    them by closed-quantity share — attributing the full amount to each row
    independently would double (or N-times) count it."""
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    trades = [
        TradeResult(
            symbol="PERP",
            entry_at=entry_at,
            exit_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
            side="short",
            entry_price=100.0,
            exit_price=100.0,
            quantity=3.0,
            gross_pnl=0.0,
            commission=0.0,
            slippage=0.0,
            tax=0.0,
            net_pnl=0.0,
            gross_return=0.0,
            net_return=0.0,
            periods_held=2,
        ),
        TradeResult(
            symbol="PERP",
            entry_at=entry_at,
            exit_at=datetime(2026, 1, 1, 3, tzinfo=UTC),
            side="short",
            entry_price=100.0,
            exit_price=100.0,
            quantity=2.0,
            gross_pnl=0.0,
            commission=0.0,
            slippage=0.0,
            tax=0.0,
            net_pnl=0.0,
            gross_return=0.0,
            net_return=0.0,
            periods_held=3,
        ),
    ]
    financing_cash_flows = [
        FinancingCashFlow(
            ts=datetime(2026, 1, 1, 1, tzinfo=UTC),
            symbol="PERP",
            side="short",
            quantity=5.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=50.0,
            group_id=None,
            entry_at=entry_at,
        )
    ]
    notionals = [300.0, 200.0]  # entry_price(100) * quantity
    lifecycle_events = [
        FinancingLifecycleEvent(
            ts=entry_at,
            symbol="PERP",
            entry_at=entry_at,
            event_type="open",
            fill_quantity=5.0,
            remaining_quantity=5.0,
        ),
        FinancingLifecycleEvent(
            ts=trades[0].exit_at,
            symbol="PERP",
            entry_at=entry_at,
            event_type="reduce",
            fill_quantity=3.0,
            remaining_quantity=2.0,
        ),
        FinancingLifecycleEvent(
            ts=trades[1].exit_at,
            symbol="PERP",
            entry_at=entry_at,
            event_type="close",
            fill_quantity=2.0,
            remaining_quantity=0.0,
        ),
    ]
    position_events = [
        PositionEvent(
            ts=event.ts,
            symbol=event.symbol,
            side="short",
            event_type=event.event_type,
            fill_quantity=event.fill_quantity,
            price=100.0,
            entry_price=100.0,
            remaining_quantity=event.remaining_quantity,
            notional=event.fill_quantity * 100.0,
            commission=0.0,
            slippage=0.0,
            tax=0.0,
            entry_at=event.entry_at,
        )
        for event in lifecycle_events
    ]

    trade_pnls = _attribute_financing_to_trades(
        trades,
        notionals,
        position_events,
        financing_cash_flows,
    )

    # Split 3:2 by closed quantity (5 total) — not 50.0 attributed to each.
    assert trade_pnls[0].net_pnl == pytest.approx(30.0)
    assert trade_pnls[1].net_pnl == pytest.approx(20.0)
    assert sum(pnl.net_pnl for pnl in trade_pnls) == pytest.approx(50.0)


def test_financing_after_partial_close_is_not_assigned_to_the_closed_trade() -> None:
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    first_exit = datetime(2026, 1, 1, 2, tzinfo=UTC)
    final_exit = datetime(2026, 1, 1, 4, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 5.0, 5.0),
        FinancingLifecycleEvent(first_exit, "PERP", entry_at, "reduce", 3.0, 2.0),
        FinancingLifecycleEvent(final_exit, "PERP", entry_at, "close", 2.0, 0.0),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=datetime(2026, 1, 1, 1, tzinfo=UTC),
            symbol="PERP",
            side="short",
            quantity=5.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=50.0,
            group_id=None,
            entry_at=entry_at,
        ),
        FinancingCashFlow(
            ts=datetime(2026, 1, 1, 3, tzinfo=UTC),
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        ),
    ]

    attributed = attribute_financing_to_closes(events, cash_flows)

    assert attributed == pytest.approx([30.0, 40.0])


def test_financing_after_a_full_close_is_refused_rather_than_dropped() -> None:
    """A flow can only accrue to quantity that is open, so one arriving after
    the position is flat has nowhere to land.

    This is the intra-run close: it fills during the bar's execution, and the
    accrual that follows covers only the positions that survived it, so a flow
    landing here means the producer's ordering is broken. (The end-of-run
    force close is the other way round, and says so -- see
    ``after_bar_accrual``.) The allocator is loud about every other broken
    invariant, and silently discarding money is the one failure that would not
    show up as a wrong number anywhere.
    """
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    exit_at = datetime(2026, 1, 1, 4, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 2.0, 2.0),
        FinancingLifecycleEvent(exit_at, "PERP", entry_at, "close", 2.0, 0.0),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=exit_at,
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        )
    ]

    with pytest.raises(ValueError, match="closed position"):
        attribute_financing_to_closes(events, cash_flows)


def test_an_end_of_run_close_takes_the_financing_of_the_bar_it_closes_on() -> None:
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    exit_at = datetime(2026, 1, 1, 4, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 2.0, 2.0),
        FinancingLifecycleEvent(
            exit_at,
            "PERP",
            entry_at,
            "close",
            2.0,
            0.0,
            after_bar_accrual=True,
        ),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=exit_at,
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        )
    ]

    assert attribute_financing_to_closes(events, cash_flows) == pytest.approx([20.0])


def test_a_residual_leaving_terminal_close_splits_the_accrual_it_follows() -> None:
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    capped_exit = datetime(2026, 1, 1, 4, tzinfo=UTC)
    residual_accrual = datetime(2026, 1, 1, 5, tzinfo=UTC)
    residual_exit = datetime(2026, 1, 1, 6, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 2.0, 2.0),
        FinancingLifecycleEvent(
            capped_exit,
            "PERP",
            entry_at,
            "reduce",
            1.0,
            1.0,
            after_bar_accrual=True,
        ),
        FinancingLifecycleEvent(residual_exit, "PERP", entry_at, "close", 1.0, 0.0),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=capped_exit,
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        ),
        FinancingCashFlow(
            ts=residual_accrual,
            symbol="PERP",
            side="short",
            quantity=1.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=10.0,
            group_id=None,
            entry_at=entry_at,
        ),
    ]

    # Half the position is closed against a balance that already holds the
    # accrual, and the residual keeps the other half plus what it earns after.
    assert attribute_financing_to_closes(events, cash_flows) == pytest.approx([10.0, 20.0])


def test_an_open_precedes_financing_at_the_same_timestamp() -> None:
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    exit_at = datetime(2026, 1, 1, 4, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 2.0, 2.0),
        FinancingLifecycleEvent(exit_at, "PERP", entry_at, "close", 2.0, 0.0),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=entry_at,
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        )
    ]

    # The balance is opened by the fill, so the flow lands on it rather than
    # on a fresh balance the open would then contradict.
    assert attribute_financing_to_closes(events, cash_flows) == pytest.approx([20.0])


def test_financing_pool_tracks_scale_in_and_same_timestamp_close_order() -> None:
    entry_at = datetime(2026, 1, 1, tzinfo=UTC)
    first_exit = datetime(2026, 1, 1, 3, tzinfo=UTC)
    events = [
        FinancingLifecycleEvent(entry_at, "PERP", entry_at, "open", 2.0, 2.0),
        FinancingLifecycleEvent(
            datetime(2026, 1, 1, 2, tzinfo=UTC),
            "PERP",
            entry_at,
            "add",
            2.0,
            4.0,
        ),
        FinancingLifecycleEvent(first_exit, "PERP", entry_at, "reduce", 1.0, 3.0),
        FinancingLifecycleEvent(
            datetime(2026, 1, 1, 5, tzinfo=UTC),
            "PERP",
            entry_at,
            "close",
            3.0,
            0.0,
        ),
    ]
    cash_flows = [
        FinancingCashFlow(
            ts=datetime(2026, 1, 1, 1, tzinfo=UTC),
            symbol="PERP",
            side="short",
            quantity=2.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=20.0,
            group_id=None,
            entry_at=entry_at,
        ),
        FinancingCashFlow(
            ts=first_exit,
            symbol="PERP",
            side="short",
            quantity=3.0,
            mark_price=100.0,
            multiplier=1.0,
            rate=0.01,
            cash_flow=30.0,
            group_id=None,
            entry_at=entry_at,
        ),
    ]

    attributed = attribute_financing_to_closes(events, cash_flows)

    # The first flow is pooled across four units after scale-in, so closing
    # one releases 5. The same-timestamp flow accrues after that reduction
    # and belongs entirely to the remaining three units.
    assert attributed == pytest.approx([5.0, 45.0])


# ---------------------------------------------------------------------------
# Short borrow
# ---------------------------------------------------------------------------


def _borrow_bar(rate: float | None = 0.0001) -> dict[str, dict[str, object]]:
    return {"PERP": {"close": 100.0, "borrow_rate": rate}}


def test_only_shorts_pay_borrow_interest() -> None:
    ts = datetime(2026, 1, 1, 8, tzinfo=UTC)

    _, short_flows = calculate_borrow_cash_flows(
        ts,
        _borrow_bar(),
        {"PERP": _position("short")},
        get_cost_model=lambda _symbol: _cost_model(),
    )
    _, long_flows = calculate_borrow_cash_flows(
        ts,
        _borrow_bar(),
        {"PERP": _position("long")},
        get_cost_model=lambda _symbol: _cost_model(),
    )

    # 2 units * 100 * multiplier 10 * 0.0001, paid out.
    assert [flow.cash_flow for flow in short_flows] == [-0.2]
    assert short_flows[0].kind == "borrow"
    assert long_flows == []


def test_missing_borrow_rate_is_not_free() -> None:
    ts = datetime(2026, 1, 1, 8, tzinfo=UTC)
    positions = {"PERP": _position("short")}
    get_cost_model = lambda _symbol: _cost_model()  # noqa: E731

    observed, flows = calculate_borrow_cash_flows(
        ts, _borrow_bar(None), positions, get_cost_model=get_cost_model
    )
    assert (observed, flows) == ((), [])

    observed, flows = calculate_borrow_cash_flows(
        ts, {"PERP": {"close": 100.0}}, positions, get_cost_model=get_cost_model
    )
    assert (observed, flows) == ((), [])


def test_borrow_cost_is_charged_to_the_short_and_folded_into_the_closed_trade() -> None:
    """The mirror of the funding test above: same short, same 1% rate, but
    borrow interest is a cost the short pays rather than income it receives,
    so the identical setup flips the round-trip from a winner to a loser."""
    data = _backtest_frame([np.nan] * 5).drop(columns=["funding_rate"])
    data["borrow_rate"] = [np.nan, 0.01, np.nan, np.nan, np.nan]

    backtest = Backtest(
        data,
        _OpenThenClose(side="short", n_bars=5),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    backtest.run()
    output = backtest.build_output()

    assert [item.cash_flow for item in output.financing_cash_flows] == pytest.approx([-20.0])
    assert [item.kind for item in output.financing_cash_flows] == ["borrow"]
    assert output.metrics.trades == 1
    assert output.metrics.win_rate == pytest.approx(0.0)
    assert output.metrics.avg_trade_return == pytest.approx(-0.01)


def test_a_long_pays_no_borrow_interest_over_a_whole_backtest() -> None:
    data = _backtest_frame([np.nan] * 5).drop(columns=["funding_rate"])
    data["borrow_rate"] = [np.nan, 0.01, np.nan, np.nan, np.nan]

    backtest = Backtest(
        data,
        _OpenThenClose(side="long", n_bars=5),
        initial_balance=10_000.0,
        cost_model=_cost_model(),
        data_source="test",
        primary_subscriptions=_subscriptions(data),
    )
    backtest.run()

    assert backtest.build_output().financing_cash_flows == ()


# ---------------------------------------------------------------------------
# attach_borrow_rate — the one place backtest and live agree on the rules
# ---------------------------------------------------------------------------


def _rate_series(timestamps: list[str], rates: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": pd.to_datetime(timestamps, utc=True),
            "borrow_rate": rates,
            "rate_period_seconds": [86_400.0] * len(timestamps),
        }
    )


def _bar_frame(timestamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"ts": pd.to_datetime(timestamps, utc=True), "close": 100.0})


def test_attach_borrow_rate_scales_a_quoted_rate_to_the_bar() -> None:
    bars = _bar_frame(["2026-01-01T00:00:00Z", "2026-01-01T08:00:00Z"])
    rates = _rate_series(["2025-12-31T00:00:00Z"], [0.00024])

    attached = attach_borrow_rate(bars, rates, timeframe="8h")

    # A daily rate over an 8h bar is a third of a day.
    assert attached["borrow_rate"].tolist() == [pytest.approx(0.00008)] * 2


def test_attach_borrow_rate_carries_one_publication_across_later_bars() -> None:
    bars = _bar_frame([f"2026-01-0{day}T00:00:00Z" for day in (1, 2, 3)])
    rates = _rate_series(["2026-01-01T00:00:00Z"], [0.001])

    attached = attach_borrow_rate(bars, rates, timeframe="1d")

    assert not attached["borrow_rate"].isna().any()


def test_attach_borrow_rate_stops_carrying_past_the_staleness_bound() -> None:
    stale_days = BORROW_RATE_MAX_AGE_QUOTING_PERIODS + 1
    bars = _bar_frame(["2026-01-10T00:00:00Z"])
    rates = _rate_series(
        [(pd.Timestamp("2026-01-10T00:00:00Z") - pd.Timedelta(days=stale_days)).isoformat()],
        [0.001],
    )

    attached = attach_borrow_rate(bars, rates, timeframe="1d")

    assert attached["borrow_rate"].isna().all()


def test_attach_borrow_rate_leaves_bars_alone_when_there_are_no_rates() -> None:
    bars = _bar_frame(["2026-01-01T00:00:00Z"])
    empty = _rate_series([], [])

    attached = attach_borrow_rate(bars, empty, timeframe="1d")

    assert "borrow_rate" not in attached.columns


def _settlements(*times: str, rate: float = 0.001) -> pd.DataFrame:
    return pd.DataFrame(
        {"ts": pd.to_datetime(list(times), utc=True, format="ISO8601"), "funding_rate": rate}
    )


def test_every_settlement_inside_a_coarse_bar_is_paid_in_it() -> None:
    """A daily bar spans three 8h settlements; none may be dropped."""
    bars = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC")})
    rates = _settlements("2026-01-01T00:00:00.003Z", "2026-01-01T08:00:00Z", "2026-01-01T16:00:00Z")

    attached = attach_funding_rate(bars, rates, timeframe="1d")

    assert attached["funding_rate"].iloc[0] == pytest.approx(0.003)
    assert attached["funding_settled_at"].iloc[0] == pd.Timestamp("2026-01-01T16:00:00Z")
    assert attached["funding_rate"].iloc[1:].isna().all()


def test_a_settlement_is_never_charged_on_two_one_minute_bars() -> None:
    bars = pd.DataFrame({"ts": pd.date_range("2026-01-01T07:59", periods=3, freq="min", tz="UTC")})

    attached = attach_funding_rate(bars, _settlements("2026-01-01T08:00:00.003Z"), timeframe="1m")

    assert attached["funding_rate"].notna().tolist() == [False, True, False]


def test_a_settlement_in_a_bar_gap_is_paid_on_the_next_bar() -> None:
    bars = pd.DataFrame(
        {"ts": pd.to_datetime(["2026-01-01T06:00Z", "2026-01-01T09:00Z"], utc=True)}
    )

    attached = attach_funding_rate(bars, _settlements("2026-01-01T08:00Z"), timeframe="1h")

    assert attached["funding_rate"].notna().tolist() == [False, True]


def test_backtest_refuses_a_forward_filled_funding_rate() -> None:
    data = _backtest_frame([0.01] * 5)
    data["funding_settled_at"] = data["funding_settled_at"].iloc[0]

    with pytest.raises(ValueError, match="forward-filled"):
        Backtest(
            data,
            _OpenOnce(),
            cost_model=_cost_model(),
            primary_subscriptions=_subscriptions(data),
        ).run()


def test_backtest_refuses_a_rate_without_its_settlement_time() -> None:
    data = _backtest_frame([0.01] * 5).drop(columns=["funding_settled_at"])

    with pytest.raises(ValueError, match="attach_funding_rate"):
        Backtest(
            data,
            _OpenOnce(),
            cost_model=_cost_model(),
            primary_subscriptions=_subscriptions(data),
        ).run()


def test_backtest_refuses_funding_on_a_non_perpetual() -> None:
    data = _backtest_frame([np.nan, 0.01, np.nan, np.nan, np.nan])

    with pytest.raises(ValueError, match="only a contract_perpetual pays funding"):
        Backtest(
            data,
            _OpenOnce(),
            cost_model=_cost_model(),
            primary_subscriptions=primary_subscriptions_for(data, "H1"),
        ).run()


def test_backtest_refuses_an_unfunded_perpetual() -> None:
    """Running without funding cost is a visible data choice: zero rates."""
    unfunded = _backtest_frame([np.nan] * 5).drop(columns=["funding_rate", "funding_settled_at"])
    subscriptions = primary_subscriptions_for(unfunded, "H1", instrument_type="contract_perpetual")

    with pytest.raises(ValueError, match="no funding_rate observations"):
        Backtest(
            unfunded, _OpenOnce(), cost_model=_cost_model(), primary_subscriptions=subscriptions
        ).run()

    zero = _backtest_frame([0.0] * 5)
    result = Backtest(
        zero, _OpenOnce(), cost_model=_cost_model(), primary_subscriptions=subscriptions
    ).run()
    assert {item.cash_flow for item in result.financing_cash_flows} == {0.0}


def test_a_missing_rate_or_a_repeated_settlement_is_refused() -> None:
    bars = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC")})
    missing = _settlements("2026-01-01T00:30Z", rate=np.nan)
    repeated = _settlements("2026-01-01T00:30Z", "2026-01-01T00:30Z")

    with pytest.raises(ValueError, match="must all carry a funding_rate"):
        attach_funding_rate(bars, missing, timeframe="1h")
    with pytest.raises(ValueError, match="must be unique"):
        attach_funding_rate(bars, repeated, timeframe="1h")


def test_backtest_refuses_a_settlement_placed_a_bar_late() -> None:
    """Rounding 07:59:59.997 to the hour moves it onto the next bar."""
    data = _backtest_frame([np.nan, 0.01, np.nan, np.nan, np.nan])
    late = data.index.get_level_values("datetime")[1] - pd.Timedelta(minutes=30)
    data.loc[data["funding_rate"].notna(), "funding_settled_at"] = late

    with pytest.raises(ValueError, match="on the wrong bar"):
        Backtest(
            data,
            _OpenOnce(),
            cost_model=_cost_model(),
            primary_subscriptions=_subscriptions(data),
        ).run()


# ---------------------------------------------------------------------------
# Sim pays each settlement once
# ---------------------------------------------------------------------------


class _PerpetualVenue:
    """A perpetual's bars and funding settlements as a venue publishes them.

    A bar is served once it has closed, and a settlement once it is
    published: at its settlement time unless ``published_at`` holds it back.
    """

    def __init__(
        self,
        clock,
        *,
        timeframe: str,
        bars: int,
        settlements: dict[str, float],
        published_at: dict[str, str] | None = None,
    ) -> None:
        self._clock = clock
        self._interval = interval_to_timedelta(timeframe)
        self._starts = pd.date_range("2026-01-01", periods=bars, freq=self._interval, tz="UTC")
        published_at = published_at or {}
        self._settlements = pd.DataFrame(
            {
                "ts": pd.to_datetime(list(settlements), utc=True, format="ISO8601"),
                "funding_rate": list(settlements.values()),
            }
        )
        self._published_at = pd.to_datetime(
            [published_at.get(at, at) for at in settlements], utc=True, format="ISO8601"
        )

    def fetch_ohlcv(self, symbol, timeframe, limit=None, **_kwargs) -> pd.DataFrame:
        closed = self._starts[self._starts + self._interval <= self._clock()]
        bars = pd.DataFrame(
            {
                "ts": closed,
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
            }
        )
        return bars.iloc[-limit:] if limit else bars

    def fetch_funding_rate_history(self, symbol, limit=None, *, since=None) -> pd.DataFrame:
        shown = (self._published_at <= self._clock()) & (
            self._settlements["ts"] >= pd.Timestamp(since, unit="ms", tz="UTC")
        )
        return self._settlements[shown].reset_index(drop=True)


def _funded_sim(
    adapter,
    clock,
    store,
    recorded,
    *,
    timeframe: str,
    feature_fn=lambda frame: frame,
    warmup_periods: int = 1,
    warmup_fetcher=None,
) -> LiveTrader:
    runner = LiveTrader(
        _OpenOnce(),
        feature_fn,
        config=make_test_cfg(
            symbols=["PERP"],
            timeframe=timeframe,
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None, warmup_periods=warmup_periods
            ),
            instrument_overrides={
                "PERP": {
                    "instrument_type": "contract_perpetual",
                    "currency": "USDT",
                    "calendar_id": "24/7",
                }
            },
        ),
        adapter=adapter,
        cost_model=_cost_model(),
        state_store=store,
        on_bar=None,
        on_position_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        on_financing_cash_flow=recorded.append,
        warmup_fetcher=warmup_fetcher,
        notifier=None,
        clock=clock,
    )
    runner._sleep = lambda _seconds: None
    return runner


def _poll_at(runner: LiveTrader, now: dict, at: str) -> None:
    """Poll just after ``at``, once the bar ending there is published."""
    now["now"] = pd.Timestamp(at).to_pydatetime() + timedelta(seconds=5)
    runner._poll_cycle()


def test_shadow_simulation_applies_and_checkpoints_funding_once() -> None:
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=3,
        settlements={"2026-01-01T01:00Z": 0.01, "2026-01-01T02:00Z": -0.005},
    )
    store = MemoryLiveStateStore()
    recorded: list[FinancingCashFlow] = []
    runner = _funded_sim(venue, lambda: now["now"], store, recorded, timeframe="H1")
    for hour in ("01", "02", "03"):
        _poll_at(runner, now, f"2026-01-01T{hour}:00Z")

    assert runner._cash == pytest.approx(99_790.0)
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])

    restored = _funded_sim(venue, lambda: now["now"], store, recorded, timeframe="H1")
    _poll_at(restored, now, "2026-01-01T03:00Z")

    assert restored._cash == pytest.approx(99_790.0)
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])


def test_shadow_simulation_pays_a_late_published_settlement_on_the_next_bar() -> None:
    """The venue publishes the 02:59:59.997 settlement after the 02:00 bar was
    processed; it is paid at 03:00 instead of never."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=4,
        settlements={"2026-01-01T01:10Z": 0.01, "2026-01-01T02:59:59.997Z": -0.005},
        published_at={"2026-01-01T02:59:59.997Z": "2026-01-01T03:30Z"},
    )
    recorded: list[FinancingCashFlow] = []
    runner = _funded_sim(
        venue, lambda: now["now"], MemoryLiveStateStore(), recorded, timeframe="H1"
    )
    for hour in ("01", "02", "03", "04"):
        _poll_at(runner, now, f"2026-01-01T{hour}:00Z")

    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])
    assert [pd.Timestamp(item.ts) for item in recorded] == pd.to_datetime(
        ["2026-01-01T01:00Z", "2026-01-01T03:00Z"], utc=True
    ).tolist()


def test_a_settlement_published_bars_late_is_still_paid_once() -> None:
    """Later than any fetch of recent bars reaches back: what is due is
    fetched from the paid watermark, not from the bars just fetched."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=7,
        settlements={"2026-01-01T01:30Z": 0.01},
        published_at={"2026-01-01T01:30Z": "2026-01-01T05:30Z"},
    )
    recorded: list[FinancingCashFlow] = []
    runner = _funded_sim(
        venue, lambda: now["now"], MemoryLiveStateStore(), recorded, timeframe="H1"
    )
    for hour in range(1, 8):
        _poll_at(runner, now, f"2026-01-01T{hour:02d}:00Z")

    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0])
    assert [pd.Timestamp(item.ts) for item in recorded] == [pd.Timestamp("2026-01-01T05:00Z")]


def test_bars_replayed_after_downtime_each_pay_their_own_settlements() -> None:
    """The restart's bars come from a warmup_fetcher and reach back past the
    warmup window, so the settlement fetch cannot start at those bars."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=10,
        settlements={f"2026-01-01T{hour:02d}:30Z": 0.01 for hour in range(10)},
    )

    def warmup_fetcher(symbol, timeframe, limit):
        return venue.fetch_ohlcv(symbol, timeframe, limit)

    store = MemoryLiveStateStore()
    recorded: list[FinancingCashFlow] = []

    def start() -> LiveTrader:
        return _funded_sim(
            venue,
            lambda: now["now"],
            store,
            recorded,
            timeframe="H1",
            warmup_periods=2,
            warmup_fetcher=warmup_fetcher,
        )

    runner = start()
    for hour in ("02", "03", "04"):
        _poll_at(runner, now, f"2026-01-01T{hour}:00Z")
    # Down for six bars, three times the warmup window.
    runner = start()
    for _ in range(8):
        _poll_at(runner, now, "2026-01-01T10:00Z")

    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0] * 8)
    assert [pd.Timestamp(item.ts) for item in recorded] == list(
        pd.date_range("2026-01-01T02:00Z", periods=8, freq="h")
    )


def test_a_late_first_settlement_is_paid_on_the_next_bar() -> None:
    """No settlement has been paid yet, and the next bar holds none."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=3,
        settlements={"2026-01-01T01:30Z": 0.01},
        published_at={"2026-01-01T01:30Z": "2026-01-01T02:30Z"},
    )
    recorded: list[FinancingCashFlow] = []
    runner = _funded_sim(
        venue, lambda: now["now"], MemoryLiveStateStore(), recorded, timeframe="H1"
    )
    for hour in ("01", "02", "03"):
        _poll_at(runner, now, f"2026-01-01T{hour}:00Z")

    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0])
    assert [pd.Timestamp(item.ts) for item in recorded] == [pd.Timestamp("2026-01-01T02:00Z")]


# One 4h bar holds two settlements, and the second is published only after
# that bar was processed.
_COARSE_SETTLEMENTS = {
    "2026-01-01T05:00Z": 0.01,
    "2026-01-01T07:00Z": 0.002,
    "2026-01-01T13:00Z": -0.005,
}
_LATE_SECOND = {"2026-01-01T07:00Z": "2026-01-01T09:00Z"}
_COARSE_CLOSES = [
    "2026-01-01T04:00Z",
    "2026-01-01T08:00Z",
    "2026-01-01T12:00Z",
    "2026-01-01T16:00Z",
]


@pytest.mark.parametrize(
    "restart_after",
    [None, "2026-01-01T08:00Z", "2026-01-01T12:00Z"],
    ids=["no restart", "restart before the late settlement", "restart after paying it"],
)
def test_a_late_second_settlement_in_one_bar_is_paid_once(restart_after: str | None) -> None:
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H4",
        bars=4,
        settlements=_COARSE_SETTLEMENTS,
        published_at=_LATE_SECOND,
    )
    store = MemoryLiveStateStore()
    recorded: list[FinancingCashFlow] = []
    runner = _funded_sim(venue, lambda: now["now"], store, recorded, timeframe="H4")
    for close in _COARSE_CLOSES:
        _poll_at(runner, now, close)
        if close == restart_after:
            runner = _funded_sim(venue, lambda: now["now"], store, recorded, timeframe="H4")
    # Every later poll fetches the paid settlements again.
    _poll_at(runner, now, _COARSE_CLOSES[-1])

    # Long 2 x 100 x multiplier 10: the 05:00 settlement on its own bar, the
    # 07:00 one alone on the next, never 05:00 again, then the 13:00 one.
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, -4.0, 10.0])
    assert [pd.Timestamp(item.ts) for item in recorded] == pd.to_datetime(
        ["2026-01-01T04:00Z", "2026-01-01T08:00Z", "2026-01-01T12:00Z"], utc=True
    ).tolist()
    assert runner._cash == pytest.approx(100_000.0 - 200.0 - 20.0 - 4.0 + 10.0)


def test_a_bar_shows_every_settlement_known_for_it() -> None:
    """The strategy reads the per-bar sum; a late settlement joins it."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H4",
        bars=4,
        settlements=_COARSE_SETTLEMENTS,
        published_at=_LATE_SECOND,
    )
    seen: list[pd.DataFrame] = []

    def feature_fn(history: pd.DataFrame) -> pd.DataFrame:
        seen.append(history.copy())
        return history

    runner = _funded_sim(
        venue,
        lambda: now["now"],
        MemoryLiveStateStore(),
        [],
        timeframe="H4",
        feature_fn=feature_fn,
        warmup_periods=2,
    )
    for close in _COARSE_CLOSES[:3]:
        _poll_at(runner, now, close)

    bar = pd.Timestamp("2026-01-01T04:00Z")
    before, after = (history.loc[bar] for history in seen[-2:])
    assert before["funding_rate"] == pytest.approx(0.01)
    assert after["funding_rate"] == pytest.approx(0.012)
    assert after["funding_settled_at"] == pd.Timestamp("2026-01-01T07:00Z")


def test_shadow_simulation_refuses_a_per_bar_rate_before_any_fill(caplog) -> None:
    """A bare callable source has no settlement history, so its bars'
    funding_rate would otherwise go unpaid; the bars never reach the run."""
    now: dict = {}
    venue = _PerpetualVenue(
        lambda: now["now"],
        timeframe="H1",
        bars=3,
        settlements={f"2026-01-01T{hour:02d}:30Z": 0.01 for hour in range(3)},
    )

    def bars_with_rates(_symbol, timeframe, limit, *, drop_incomplete=False):
        bars = venue.fetch_ohlcv("PERP", timeframe, limit)
        return attach_funding_rate(
            bars, venue.fetch_funding_rate_history("PERP", since=0), timeframe=timeframe
        )

    runner = _funded_sim(
        bars_with_rates, lambda: now["now"], MemoryLiveStateStore(), [], timeframe="H1"
    )
    for hour in ("01", "02", "03"):
        _poll_at(runner, now, f"2026-01-01T{hour}:00Z")

    assert runner._period_index == 0
    assert not runner._positions
    assert "has no fetch_funding_rate_history" in caplog.text
