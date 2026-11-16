"""Primaries off one bar grid: live refuses them, a backtest orders them by availability.

When one symbol's bar starts while another's is still open, the bar read at
the earlier start closes after the other symbol's next bar opens. Ordered by
bar start, a decision on it could fill before it was made, and live meets the
bars out of start order.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, MarketDataSubscription
from librae.core.cost_model import CostModel
from librae.core.market_data import require_aligned_bar_grids
from librae.core.strategy import Context, OrderIntent, PortfolioWeights, Strategy
from librae.live.engine import LiveTrader

from tests.conftest import make_test_cfg


def _subscription(symbol: str, timeframe: str, calendar_id: str, session_mode: str):
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source="fixture",
        instrument_type="spot",
    )


@pytest.mark.parametrize(
    "grids",
    [
        pytest.param(
            [("M5", "24/7", "extended"), ("M5", "XTAIFEX", "extended")],
            id="24/7 and TAIFEX five-minute bars share their starts",
        ),
        pytest.param(
            [("M30", "24/7", "extended"), ("M30", "XNYS", "regular")],
            id="XNYS regular hours open on the half hour",
        ),
        pytest.param(
            [("H1", "XNYS", "extended"), ("H1", "XNYS", "extended")],
            id="one calendar and session mode",
        ),
        pytest.param(
            [("D1", "24/7", "regular"), ("D1", "24/7", "extended")],
            id="a full-day calendar reads alike in both modes",
        ),
        pytest.param(
            [("H1", "XNYS", "regular"), ("H1", "XNAS", "regular")],
            id="two calendars with the same sessions, hourly",
        ),
        pytest.param(
            [("D1", "XNYS", "regular"), ("D1", "XNAS", "regular")],
            id="two calendars with the same sessions, daily",
        ),
        pytest.param(
            [("D1", "XNYS", "regular"), ("D1", "XNYS", "extended")],
            id="one calendar's sessions in both modes, daily",
        ),
        pytest.param(
            [("M1", "XTAIFEX", "extended"), ("M1", "XTAI", "extended")],
            id="TAIFEX minutes against an extended TAIEX minute index",
        ),
        pytest.param(
            [("M5", "XTAIFEX", "extended"), ("M5", "XTAI", "extended")],
            id="TAIFEX five minutes against an extended TAIEX index",
        ),
        pytest.param(
            [("M5", "24/7", "extended"), ("M5", "XNYS", "extended")],
            id="extended XNYS opens on the five-minute clock grid",
        ),
        pytest.param(
            [("D1", "XTAIFEX", "regular"), ("D1", "XTAIFEX_1725", "regular")],
            id="both TAIFEX calendars trade one day segment",
        ),
    ],
)
def test_aligned_grids_are_accepted(grids: list[tuple[str, str, str]]) -> None:
    require_aligned_bar_grids([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


@pytest.mark.parametrize(
    "grids",
    [
        pytest.param(
            [("D1", "24/7", "regular"), ("D1", "XNYS", "regular")],
            id="a 24/7 day spans the XNYS session",
        ),
        pytest.param(
            [("H1", "24/7", "regular"), ("H1", "XNYS", "regular")],
            id="XNYS hours start on the half hour",
        ),
        pytest.param(
            [("H1", "24/7", "extended"), ("H1", "XTAIFEX", "extended")],
            id="TAIFEX hours start at a quarter to",
        ),
        pytest.param(
            [("H1", "XNYS", "regular"), ("H1", "XNYS", "extended")],
            id="extended hours run on the clock, regular ones on the half hour",
        ),
        pytest.param(
            [("D1", "XTAIFEX", "extended"), ("D1", "XTAI", "extended")],
            id="a TAIFEX day opens the evening before the TAIEX day",
        ),
        pytest.param(
            [("H1", "XTAIFEX", "extended"), ("H1", "XTAI", "extended")],
            id="TAIFEX hours start at a quarter to, TAIEX on the hour",
        ),
        pytest.param(
            [("D1", "XTAIFEX", "overnight"), ("D1", "XTAIFEX_1725", "overnight")],
            id="the TAIFEX nights open at 15:00 and 17:25",
        ),
        pytest.param(
            [("D1", "XTAIFEX", "regular"), ("D1", "XTAI", "regular")],
            id="the TAIFEX day segment opens before the TAIEX session",
        ),
    ],
)
def test_misaligned_grids_are_refused(grids: list[tuple[str, str, str]]) -> None:
    with pytest.raises(ValueError, match="one shared grid"):
        require_aligned_bar_grids([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


def _frame(symbol: str, starts: list[str], closes: list[float]) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(starts, utc=True), name="datetime")
    close = np.array(closes, dtype=float)
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": np.maximum(close, 100.0) + 1,
            "low": 99.0,
            "close": close,
            "volume": 100.0,
        },
        index=index,
    )
    frame["symbol"] = symbol
    return frame.set_index("symbol", append=True).reorder_levels(["symbol", "datetime"])


class _Idle(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


_DAYS = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
_MIXED = (
    _subscription("A", "D1", "24/7", "regular"),
    _subscription("B", "D1", "XNYS", "regular"),
)


def _utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


def _mixed_data(a_available_at: list[str] | None = None) -> pd.DataFrame:
    """A on 24/7 days (each available at the next midnight), B on XNYS days
    (14:30-21:00 UTC in January). A's Jan 6 close jumps to 150."""
    a = _frame("A", _DAYS, [100, 150, 150, 150, 150])
    if a_available_at is not None:
        a["available_at"] = pd.to_datetime(a_available_at, utc=True)
    return pd.concat([a, _frame("B", [f"{day} 14:30" for day in _DAYS], [100] * 5)])


class _Recorder(Strategy):
    """Records every decision; buys ``buy`` once ``when(ctx)`` first holds."""

    def __init__(self, buy=(), when=lambda ctx: False) -> None:
        self.contexts: list[Context] = []
        self._buy = buy
        self._when = when
        self.bought_at: pd.Timestamp | None = None

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.contexts.append(ctx)
        if self.bought_at is None and self._when(ctx):
            self.bought_at = pd.Timestamp(ctx.ts)
            return [OrderIntent(symbol=symbol, action="long", quantity=1.0) for symbol in self._buy]
        return []


def _run(strategy: Strategy, data: pd.DataFrame, **kwargs):
    backtest = Backtest(
        data,
        strategy,
        strategy_name="grid",
        cost_model=CostModel.zero(),
        primary_subscriptions=_MIXED,
        **kwargs,
    )
    return backtest.run()


def _entries(result) -> dict[str, pd.Timestamp]:
    return {
        event.symbol: pd.Timestamp(event.ts)
        for event in result.position_events
        if event.event_type == "open"
    }


def test_a_backtest_decides_mixed_grids_in_availability_order() -> None:
    strategy = _Recorder()
    result = _run(strategy, _mixed_data())

    decided = [(ctx.ts, sorted(ctx.bars)) for ctx in strategy.contexts]
    expected = []
    for day, next_day in zip(_DAYS, [*_DAYS[1:], "2026-01-10"], strict=True):
        expected.append((_utc(f"{day} 21:00"), ["B"]))
        expected.append((_utc(next_day), ["A"]))
    assert decided == expected
    assert [point.ts for point in result.account.equity_curve] == [ts for ts, _ in expected]


def test_a_decision_on_a_24_7_day_fills_xnys_only_after_that_day_is_available() -> None:
    """A's Jan 6 bar is available at Jan 7 00:00, 9.5 hours after B's Jan 6
    bar opened; a decision on it fills B on B's first bar to open after then."""
    strategy = _Recorder(buy=("A", "B"), when=lambda ctx: ctx.bars.get("A", {}).get("close") == 150)
    result = _run(strategy, _mixed_data())

    assert strategy.bought_at == _utc("2026-01-07")
    assert _entries(result) == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}


def test_a_decision_on_an_xnys_close_fills_the_24_7_symbol_at_its_next_bar() -> None:
    strategy = _Recorder(buy=("A", "B"), when=lambda ctx: ctx.ts == _utc("2026-01-06 21:00"))
    result = _run(strategy, _mixed_data())

    assert strategy.contexts[2].bars.keys() == {"B"}
    assert _entries(result) == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}


def test_a_late_bar_holds_only_its_own_symbol(caplog: pytest.LogCaptureFixture) -> None:
    """A's Jan 6 bar is published three days late. Its later bars cannot be
    seen before it, but B keeps deciding and filling on its own bars."""
    strategy = _Recorder(buy=("B",), when=lambda ctx: ctx.ts == _utc("2026-01-06 21:00"))
    result = _run(
        strategy,
        _mixed_data(["2026-01-06", "2026-01-09", "2026-01-08", "2026-01-09", "2026-01-10"]),
    )

    a_events = [(ctx.ts, ctx.bars["A"]["close"]) for ctx in strategy.contexts if "A" in ctx.bars]
    # A's Jan 7 bar, published Jan 8, waits for its late Jan 6 bar.
    assert a_events == [
        (_utc("2026-01-06"), 100.0),
        (_utc("2026-01-09"), 150.0),
        (_utc("2026-01-10"), 150.0),
    ]
    assert _entries(result) == {"B": _utc("2026-01-07 14:30")}
    assert "1 primary bar(s) decided after their available_at, by up to 1 days" in caplog.text
    assert "['A']" in caplog.text


def _exits(result) -> dict[str, pd.Timestamp]:
    return {
        event.symbol: pd.Timestamp(event.ts)
        for event in result.position_events
        if event.event_type == "close"
    }


def test_each_position_closes_on_its_own_last_bar_at_the_end() -> None:
    """B's last bar completes before the final event, which holds two of A's
    bars; both positions close, with no residual reported."""
    strategy = _Recorder(buy=("A", "B"), when=lambda ctx: ctx.ts == _utc("2026-01-06 21:00"))
    result = _run(
        strategy,
        _mixed_data(["2026-01-06", "2026-01-07", "2026-01-08", "2026-01-10", "2026-01-10"]),
    )

    assert _exits(result) == {"A": _utc("2026-01-09"), "B": _utc("2026-01-09 14:30")}
    assert {trade.symbol for trade in result.trades} == {"A", "B"}
    assert not [event for event in result.runtime_events if event.event_type == "decision_skipped"]


def test_context_carries_each_bar_start_off_one_grid() -> None:
    strategy = _Recorder()
    _run(strategy, _mixed_data())

    ctx = strategy.contexts[1]
    assert ctx.ts == _utc("2026-01-06")
    assert ctx.market_data is not None
    assert ctx.market_data.history(_MIXED[0]).index[-1] == _utc("2026-01-05")
    assert ctx.market_data.history(_MIXED[1]).index[-1] == _utc("2026-01-05 14:30")


@pytest.mark.parametrize(
    "decision",
    [
        pytest.param(PortfolioWeights(weights={"A": 0.5, "B": 0.5}), id="portfolio weights"),
        pytest.param(
            [
                OrderIntent(symbol="A", action="long", quantity=1.0, group_id="hedge"),
                OrderIntent(symbol="B", action="short", quantity=1.0, group_id="hedge"),
            ],
            id="order group",
        ),
    ],
)
def test_legs_on_other_grids_are_refused_with_the_reason(decision) -> None:
    class _Emit(Strategy):
        def on_bar(self, ctx: Context):
            return decision

    with pytest.raises(ValueError, match="not supported across grids"):
        _run(_Emit(), _mixed_data())


def test_batch_features_on_mixed_grids_see_only_available_bars() -> None:
    seen: list[tuple[pd.Timestamp, dict[str, pd.Timestamp]]] = []

    def features(batch):
        seen.append(
            (
                pd.Timestamp(batch.event_ts),
                {
                    subscription.symbol: batch.market_data.history(subscription)[
                        "available_at"
                    ].max()
                    for subscription in batch.primary_subscriptions
                    if not batch.market_data.history(subscription).empty
                },
            )
        )
        return {
            subscription: batch.market_data.history(subscription)
            for subscription in batch.active_primary_subscriptions
        }

    strategy = _Recorder(buy=("A", "B"), when=lambda ctx: ctx.bars.get("A", {}).get("close") == 150)
    result = _run(strategy, _mixed_data(), batch_feature_fn=features)

    assert len(seen) == 2 * len(_DAYS)
    assert all(latest <= event_ts for event_ts, history in seen for latest in history.values())
    assert _entries(result) == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}


def test_a_direct_backtest_cannot_skip_the_grid_check_by_omitting_subscriptions() -> None:
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    data = pd.concat(
        [
            _frame("A", days, [100, 150, 150]),
            _frame("B", [f"{day} 14:30" for day in days], [100, 100, 100]),
        ]
    )
    with pytest.raises(ValueError, match="requires primary_subscriptions"):
        Backtest(data, _Idle(), strategy_name="grid", cost_model=CostModel.zero())


def test_live_refuses_misaligned_grids_at_construction() -> None:
    symbols = ["A", "B"]
    calendars = {"A": "24/7", "B": "XNYS"}
    with pytest.raises(ValueError, match="one shared grid"):
        LiveTrader(
            _Idle(),
            lambda frame: frame,
            config=make_test_cfg(
                mode="sim",
                symbols=symbols,
                timeframe="H1",
                session_mode="regular",
                instrument_overrides={
                    symbol: {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "calendar_id": calendars[symbol],
                    }
                    for symbol in symbols
                },
                symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in symbols},
            ),
            adapter={symbol: (lambda *_a, **_k: pd.DataFrame()) for symbol in symbols},
            cost_model=CostModel.zero(),
        )
