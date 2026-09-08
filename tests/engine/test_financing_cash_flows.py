"""Perpetual-funding and short-borrow cash-flow accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime

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
    attribute_financing_to_closes,
    calculate_borrow_cash_flows,
    calculate_funding_cash_flows,
)
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import PositionState
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore

from tests.conftest import make_test_cfg


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
            "funding_rate": funding_rates,
        },
        index=index,
    )
    if mark_prices is not None:
        data["funding_mark_price"] = mark_prices
    return data


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
    )
    backtest.run()

    output = backtest.build_output()

    assert [flow.cash_flow for flow in output.financing_cash_flows] == pytest.approx([-40.0, -20.0])
    assert output.equity_curve[-1].equity == pytest.approx(9_940.0)
    assert output.metrics.trades == 2
    assert output.metrics.avg_trade_return == pytest.approx(-0.015)


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

    The engine cannot produce this today -- financing is computed after
    execution, over the positions that survived the bar -- but the allocator
    is loud about every other broken invariant, and silently discarding money
    is the one failure that would not show up as a wrong number anywhere.
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


def test_shadow_simulation_applies_and_checkpoints_funding_once() -> None:
    timestamps = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    rates = [np.nan, 0.01, -0.005]
    frames = [
        pd.DataFrame(
            {
                "ts": timestamps[: index + 1],
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1_000.0,
                "funding_rate": rates[: index + 1],
            }
        )
        for index in range(3)
    ]
    responses = iter(frames)
    store = MemoryLiveStateStore()
    recorded = []
    config = make_test_cfg(
        symbols=["PERP"],
        execution=ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=1,
        ),
        instrument_overrides={
            "PERP": {
                "instrument_type": "contract_perpetual",
                "currency": "USDT",
                "calendar_id": "24/7",
            }
        },
    )

    runner = LiveTrader(
        _OpenOnce(),
        lambda frame: frame,
        config=config,
        adapter=lambda *_args, **_kwargs: next(responses),
        cost_model=_cost_model(),
        state_store=store,
        on_bar=None,
        on_position_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        on_financing_cash_flow=recorded.append,
        warmup_fetcher=None,
        notifier=None,
        clock=lambda: datetime(2026, 1, 1, 3, tzinfo=UTC),
    )
    runner._sleep = lambda _seconds: None
    runner.run(max_iterations=3)

    assert runner._cash == pytest.approx(99_790.0)
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])

    restored = LiveTrader(
        _OpenOnce(),
        lambda frame: frame,
        config=config,
        adapter=lambda *_args, **_kwargs: frames[-1],
        cost_model=_cost_model(),
        state_store=store,
        on_bar=None,
        on_position_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        on_financing_cash_flow=recorded.append,
        warmup_fetcher=None,
        notifier=None,
        clock=lambda: datetime(2026, 1, 1, 3, tzinfo=UTC),
    )
    restored._sleep = lambda _seconds: None
    restored.run(max_iterations=1)

    assert restored._cash == pytest.approx(99_790.0)
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])


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
