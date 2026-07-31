"""Perpetual-funding cash-flow accounting tests."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, Context, CostModel, OrderIntent, Strategy
from librae.core.funding import calculate_funding_cash_flows
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
    with pytest.raises(ValueError, match="funding"):
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

    assert [item.cash_flow for item in result.funding_cash_flows] == pytest.approx([-20.0, 10.0])
    assert result.final_equity == pytest.approx(9_990.0)
    assert output.account.net_pnl == pytest.approx(-10.0)
    assert output.metrics.total_return == pytest.approx(-0.001)
    assert [item.cash_flow for item in output.funding_cash_flows] == pytest.approx([-20.0, 10.0])
    assert all(event.price == 100.0 for event in output.order_events)


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

    assert len(result.funding_cash_flows) == 1
    assert result.funding_cash_flows[0].cash_flow == pytest.approx(11.0)
    assert result.final_equity == pytest.approx(10_011.0)


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
        instrument_overrides={
            "PERP": {
                "instrument_type": "contract_perpetual",
                "currency": "USDT",
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
        on_order_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        on_funding_cash_flow=recorded.append,
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
        on_order_event=None,
        on_ohlcv=None,
        on_heartbeat=None,
        on_signal_outcome=None,
        on_funding_cash_flow=recorded.append,
        warmup_fetcher=None,
        notifier=None,
        clock=lambda: datetime(2026, 1, 1, 3, tzinfo=UTC),
    )
    restored._sleep = lambda _seconds: None
    restored.run(max_iterations=1)

    assert restored._cash == pytest.approx(99_790.0)
    assert [item.cash_flow for item in recorded] == pytest.approx([-20.0, 10.0])
