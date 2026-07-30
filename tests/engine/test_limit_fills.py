"""Limit-order execution semantics."""

from __future__ import annotations

import logging

import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import resolve_fill_price
from librae.core.strategy import OrderIntent, Strategy


def _bar(
    *,
    open_: float,
    high: float,
    low: float,
    can_buy: bool = True,
    can_sell: bool = True,
) -> dict[str, float]:
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": open_,
        "volume": 100.0,
        "can_buy": can_buy,
        "can_sell": can_sell,
    }


@pytest.mark.parametrize(
    ("action", "position_side", "bar", "expected"),
    [
        (
            OrderIntent(action="long", symbol="X", limit_price=100.0),
            None,
            _bar(open_=105, high=106, low=99),
            100.0,
        ),
        (
            OrderIntent(action="long", symbol="X", limit_price=100.0),
            None,
            _bar(open_=95, high=98, low=94),
            95.0,
        ),
        (
            OrderIntent(action="short", symbol="X", limit_price=100.0),
            None,
            _bar(open_=95, high=101, low=94),
            100.0,
        ),
        (
            OrderIntent(action="short", symbol="X", limit_price=100.0),
            None,
            _bar(open_=105, high=106, low=104),
            105.0,
        ),
        (
            OrderIntent(action="close", symbol="X", limit_price=100.0),
            "long",
            _bar(open_=105, high=106, low=104),
            105.0,
        ),
        (
            OrderIntent(action="close", symbol="X", limit_price=100.0),
            "short",
            _bar(open_=95, high=98, low=94),
            95.0,
        ),
    ],
)
def test_limit_fill_is_side_correct(action, position_side, bar, expected) -> None:
    assert resolve_fill_price(
        bar,
        action,
        default_fill="open",
        position_side=position_side,
    ) == pytest.approx(expected)


def test_unreached_limit_expires_with_observable_log(caplog) -> None:
    action = OrderIntent(action="long", symbol="X", limit_price=100.0)

    with caplog.at_level(logging.INFO, logger="librae.core.executor"):
        fill = resolve_fill_price(
            _bar(open_=105.0, high=106.0, low=101.0),
            action,
            default_fill="open",
        )

    assert fill is None
    assert "expired unfilled" in caplog.text


@pytest.mark.parametrize(
    ("action", "position_side", "bar"),
    [
        (
            OrderIntent(action="long", symbol="X"),
            None,
            _bar(open_=110, high=110, low=110, can_buy=False),
        ),
        (
            OrderIntent(action="short", symbol="X"),
            None,
            _bar(open_=90, high=90, low=90, can_sell=False),
        ),
        (
            OrderIntent(action="close", symbol="X"),
            "short",
            _bar(open_=110, high=110, low=110, can_buy=False),
        ),
        (
            OrderIntent(action="close", symbol="X"),
            "long",
            _bar(open_=90, high=90, low=90, can_sell=False),
        ),
    ],
)
def test_untradable_order_side_rejects_fill(
    action,
    position_side,
    bar,
) -> None:
    assert (
        resolve_fill_price(
            bar,
            action,
            default_fill="open",
            position_side=position_side,
        )
        is None
    )


def test_partial_side_tradability_fails_instead_of_defaulting() -> None:
    bar = _bar(open_=100, high=101, low=99)
    del bar["can_sell"]

    with pytest.raises(ValueError, match="can_buy and can_sell together"):
        resolve_fill_price(
            bar,
            OrderIntent(action="long", symbol="X"),
            default_fill="open",
        )


@pytest.mark.parametrize(
    ("action", "position_side", "bar"),
    [
        (
            OrderIntent(action="short", symbol="X"),
            None,
            _bar(open_=110, high=110, low=110, can_buy=False),
        ),
        (
            OrderIntent(action="long", symbol="X"),
            None,
            _bar(open_=90, high=90, low=90, can_sell=False),
        ),
        (
            OrderIntent(action="close", symbol="X"),
            "long",
            _bar(open_=110, high=110, low=110, can_buy=False),
        ),
        (
            OrderIntent(action="close", symbol="X"),
            "short",
            _bar(open_=90, high=90, low=90, can_sell=False),
        ),
    ],
)
def test_opposite_order_side_remains_tradable(
    action,
    position_side,
    bar,
) -> None:
    assert resolve_fill_price(
        bar,
        action,
        default_fill="open",
        position_side=position_side,
    ) == pytest.approx(bar["open"])


def test_unfilled_limit_does_not_roll_to_a_later_bar(caplog) -> None:
    prices = [
        _bar(open_=105.0, high=106.0, low=104.0),
        _bar(open_=105.0, high=106.0, low=101.0),
        _bar(open_=95.0, high=96.0, low=90.0),
        _bar(open_=95.0, high=96.0, low=94.0),
        _bar(open_=95.0, high=96.0, low=94.0),
    ]
    timeline = pd.date_range("2025-01-01", periods=len(prices), freq="h", tz="UTC")
    frame = pd.DataFrame(prices)
    frame.index = pd.MultiIndex.from_arrays(
        [["X"] * len(frame), timeline],
        names=["symbol", "datetime"],
    )

    class SubmitOnce(Strategy):
        def on_bar(self, ctx):
            if ctx.period_index == 0:
                return [OrderIntent(action="long", symbol="X", quantity=1.0, limit_price=100.0)]
            return []

    with caplog.at_level(logging.INFO, logger="librae.core.executor"):
        result = Backtest(
            frame,
            SubmitOnce(),
            initial_balance=1_000.0,
            cost_model=CostModel.zero(),
            data_source="test",
        ).run()

    assert result.order_events == []
    assert result.trades == []
    assert "expired unfilled" in caplog.text


def test_resting_limit_protection_starts_on_next_bar() -> None:
    prices = [
        _bar(open_=100.0, high=101.0, low=99.0),
        _bar(open_=100.0, high=110.0, low=90.0),
        _bar(open_=100.0, high=106.0, low=99.0),
        _bar(open_=105.0, high=106.0, low=104.0),
        _bar(open_=105.0, high=106.0, low=104.0),
    ]
    timeline = pd.date_range("2025-01-01", periods=len(prices), freq="h", tz="UTC")
    frame = pd.DataFrame(prices)
    frame.index = pd.MultiIndex.from_arrays(
        [["X"] * len(frame), timeline],
        names=["symbol", "datetime"],
    )

    class LimitWithTarget(Strategy):
        def on_bar(self, ctx):
            if ctx.period_index == 0:
                return [
                    OrderIntent(
                        action="long",
                        symbol="X",
                        quantity=1.0,
                        limit_price=95.0,
                        take_profit_price=105.0,
                    )
                ]
            return []

    result = Backtest(
        frame,
        LimitWithTarget(),
        initial_balance=1_000.0,
        cost_model=CostModel.zero(),
        data_source="test",
    ).run()

    assert result.trades[0].entry_at == timeline[1].to_pydatetime()
    assert result.trades[0].exit_at == timeline[2].to_pydatetime()
    assert result.trades[0].exit_price == pytest.approx(105.0)
