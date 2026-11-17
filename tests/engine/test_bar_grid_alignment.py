"""Primaries off one bar grid: every mode orders their events by availability.

When one symbol's bar starts while another's is still open, the bar read at
the earlier start closes after the other symbol's next bar opens. Ordered by
bar start, a decision on it could fill before it was made. Sim runs the same
events as a backtest and fills them on the same bars.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, MarketDataSubscription
from librae.core.cost_model import CostModel
from librae.core.market_data import bar_grids_align
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import Context, OrderIntent, PortfolioWeights, Strategy
from librae.live.engine import LiveTrader
from librae.live.state import MemoryLiveStateStore

from tests.conftest import declare_instruments, make_test_cfg
from tests.engine.live_factories import halt_file_of
from tests.engine.test_live_runner import _broker_report, _mock_order_adapter


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
def test_aligned_grids_are_recognized(grids: list[tuple[str, str, str]]) -> None:
    assert bar_grids_align([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


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
def test_misaligned_grids_are_recognized(grids: list[tuple[str, str, str]]) -> None:
    assert not bar_grids_align([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


def _frame(symbol: str, starts: list[str], closes: list[float]) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(starts, utc=True), name="datetime")
    close = np.array(closes, dtype=float)
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": np.maximum(close, 100.0) + 1,
            "low": np.minimum(close, 100.0) - 1,
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

    def __init__(self, buy=(), when=lambda ctx: False, quantity: float = 1.0) -> None:
        self.contexts: list[Context] = []
        self._buy = buy
        self._when = when
        self._quantity = quantity
        self.bought_at: pd.Timestamp | None = None

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.contexts.append(ctx)
        if self.bought_at is None and self._when(ctx):
            self.bought_at = pd.Timestamp(ctx.ts)
            return [
                OrderIntent(symbol=symbol, action="long", quantity=self._quantity)
                for symbol in self._buy
            ]
        return []


def _run(strategy: Strategy, data: pd.DataFrame, **kwargs):
    backtest = Backtest(
        data,
        strategy,
        strategy_name="grid",
        cost_model=CostModel.zero(),
        primary_subscriptions=_MIXED,
        instrument_overrides=declare_instruments(data),
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
        Backtest(
            data,
            _Idle(),
            strategy_name="grid",
            cost_model=CostModel.zero(),
            instrument_overrides=declare_instruments(data),
        )


_EXECUTION = ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=1)


class _Runtime:
    """Sim, or live with an ``order_adapter``, over ``data``, polled a minute
    after each instant a bar is published or at ``polls``; each poll sees the
    bars published by then, as a feed serves them."""

    def __init__(self, strategy: Strategy, data: pd.DataFrame, polls=None, **kwargs) -> None:
        symbols = ["A", "B"]
        calendars = {"A": "24/7", "B": "XNYS"}
        frames = {
            symbol: data.xs(symbol, level="symbol")
            .reset_index()
            .rename(columns={"datetime": "ts"})
            .dropna(axis=1, how="all")
            for symbol in symbols
        }
        batch_feature_fn = kwargs.pop("batch_feature_fn", None)
        feature_fn = kwargs.pop("feature_fn", lambda frame: frame)
        state_store = kwargs.pop("state_store", None)
        live: dict = {}
        if (order_adapter := kwargs.pop("order_adapter", None)) is not None:
            state_store = MemoryLiveStateStore(restart_durable_for_tests=True)
            live = {
                "order_adapter": order_adapter,
                "runtime_revision": "grid",
                "halt_file": halt_file_of(state_store),
            }
            # Reconciliation reads the mock's flat book; these tests are about events.
            kwargs.update(broker="binance", reconciliation_interval_seconds=10**7)
        polls = polls if polls is not None else _publications()
        self._clock = {"now": polls[0].to_pydatetime()}
        self.trader = LiveTrader(
            strategy,
            None if batch_feature_fn is not None else feature_fn,
            batch_feature_fn=batch_feature_fn,
            state_store=state_store,
            **live,
            config=make_test_cfg(
                mode="live" if live else "sim",
                symbols=symbols,
                timeframe="D1",
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
                execution=_EXECUTION,
                **kwargs,
            ),
            adapter={
                symbol: (
                    lambda *_args, symbol=symbol, **_kwargs: (
                        frames[symbol]
                        .loc[
                            frames[symbol]["ts"]
                            < self.withheld.get(symbol, pd.Timestamp.max.tz_localize("UTC"))
                        ]
                        .copy()
                    )
                )
                for symbol in symbols
            },
            cost_model=CostModel.zero(),
            clock=lambda: self._clock["now"],
        )
        # Bars of a symbol from this start on, which its feed has not served yet.
        self.withheld: dict[str, pd.Timestamp] = {}
        self.position_events: list = []
        self.equity_ts: list[pd.Timestamp] = []
        self.trader._on_position_event = lambda event, _sequence: self.position_events.append(event)
        self.trader._on_bar = lambda _run_id, ts, *_rest: self.equity_ts.append(pd.Timestamp(ts))
        self.trader._last_reconciliation_at = self._clock["now"]
        for now in polls:
            self.poll(now)

    def poll(self, now: pd.Timestamp) -> None:
        self._clock["now"] = now.to_pydatetime()
        self.trader._poll_cycle()

    def entries(self) -> dict[str, pd.Timestamp]:
        return self._of("open")

    def exits(self) -> dict[str, pd.Timestamp]:
        return self._of("close")

    def _of(self, event_type: str) -> dict[str, pd.Timestamp]:
        return {
            event.symbol: pd.Timestamp(event.ts)
            for event in self.position_events
            if event.event_type == event_type
        }


def _publications() -> list[pd.Timestamp]:
    """A minute after each instant bars are published, from the first at
    which both symbols have one. A is available at the next midnight, B at
    the XNYS close."""
    available = {
        "A": [pd.Timestamp(day, tz="UTC") + pd.Timedelta(days=1) for day in _DAYS],
        "B": [_utc(f"{day} 21:00") for day in _DAYS],
    }
    first = max(times[0] for times in available.values())
    instants = sorted({at for times in available.values() for at in times if at >= first})
    return [at + pd.Timedelta(minutes=1) for at in instants]


def _decisions(strategy: _Recorder) -> list[tuple[pd.Timestamp, list[str]]]:
    return [(pd.Timestamp(ctx.ts), sorted(ctx.bars)) for ctx in strategy.contexts]


@pytest.mark.parametrize(
    "when",
    [
        pytest.param(
            lambda ctx: ctx.bars.get("A", {}).get("close") == 150, id="decided on a 24/7 day"
        ),
        pytest.param(lambda ctx: ctx.ts == _utc("2026-01-06 21:00"), id="decided on an XNYS close"),
    ],
)
def test_sim_decides_and_fills_mixed_grids_as_a_backtest_does(when) -> None:
    """Decided on the XNYS close at 21:00, A fills on its next day, not on
    the one already open then."""
    backtest_strategy = _Recorder(buy=("A", "B"), when=when)
    result = _run(backtest_strategy, _mixed_data(), execution=_EXECUTION)
    sim_strategy = _Recorder(buy=("A", "B"), when=when)
    sim = _Runtime(sim_strategy, _mixed_data())

    assert _decisions(sim_strategy) == _decisions(backtest_strategy)
    assert sim.entries() == _entries(result)
    assert _entries(result) == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}
    assert sim.equity_ts == [point.ts for point in result.account.equity_curve]
    assert sim.trader._last_cycle_ts == _utc("2026-01-10")


def test_a_restart_keeps_when_a_pending_intent_was_decided() -> None:
    """Restarted after the 21:00 decision, sim still fills A on its next
    day rather than on the day open at 21:00."""
    when = lambda ctx: ctx.ts == _utc("2026-01-06 21:00")  # noqa: E731
    polls = _publications()
    store = MemoryLiveStateStore()
    first = _Runtime(
        _Recorder(buy=("A", "B"), when=when),
        _mixed_data(),
        [now for now in polls if now <= _utc("2026-01-06 21:01")],
        state_store=store,
    )
    assert first.entries() == {}
    restarted = _Runtime(
        _Recorder(),
        _mixed_data(),
        [now for now in polls if now > _utc("2026-01-06 21:01")],
        state_store=store,
    )

    assert restarted.entries() == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}


def test_live_sends_each_order_when_its_symbol_is_decided() -> None:
    """Decided on the XNYS close, B's order goes out at once; A has no bar
    in that event, so its order goes out once A's next bar is decided."""
    placed: list[tuple[str, pd.Timestamp]] = []
    adapter = _mock_order_adapter()
    sim: _Runtime | None = None

    def place(signal):
        assert sim is not None
        placed.append((signal["canonical_symbol"], pd.Timestamp(sim._clock["now"])))
        return _broker_report(order_id=signal["client_order_id"], quantity=signal["quantity"])

    adapter.place_order.side_effect = place
    strategy = _Recorder(buy=("A", "B"), when=lambda ctx: ctx.ts == _utc("2026-01-06 21:00"))
    polls = _publications()
    sim = _Runtime(strategy, _mixed_data(), polls[:1], order_adapter=adapter)
    for now in polls[1:]:
        sim.poll(now)

    backtest_strategy = _Recorder()
    _run(backtest_strategy, _mixed_data(), execution=_EXECUTION)
    assert _decisions(strategy) == _decisions(backtest_strategy)
    assert placed == [("B", _utc("2026-01-06 21:01")), ("A", _utc("2026-01-07 00:01"))]


def _b_late_on_jan_6() -> tuple[_Runtime, _Recorder]:
    """B's Jan 6 bar, due at 21:00, is not served when A's Jan 6 bar becomes
    available at midnight."""
    strategy = _Recorder()
    sim = _Runtime(strategy, _mixed_data(), [_utc("2026-01-06 00:01")])
    sim.withheld["B"] = _utc("2026-01-06 14:30")
    sim.poll(_utc("2026-01-06 21:01"))
    sim.poll(_utc("2026-01-07 00:00:05"))
    return sim, strategy


def test_an_event_waits_for_a_symbol_due_before_it() -> None:
    sim, strategy = _b_late_on_jan_6()
    assert len(strategy.contexts) == 2

    sim.withheld.clear()
    sim.poll(_utc("2026-01-07 00:00:20"))

    assert _decisions(strategy)[2:] == [
        (_utc("2026-01-06 21:00"), ["B"]),
        (_utc("2026-01-07"), ["A"]),
    ]


def test_past_the_hold_an_event_goes_ahead_and_the_late_bar_follows() -> None:
    sim, strategy = _b_late_on_jan_6()
    sim.poll(_utc("2026-01-07 00:00:35"))
    sim.withheld.clear()
    sim.poll(_utc("2026-01-07 00:01"))

    assert _decisions(strategy)[2:] == [
        (_utc("2026-01-07"), ["A"]),
        (_utc("2026-01-06 21:00"), ["B"]),
    ]
    assert sim.trader._last_cycle_ts == _utc("2026-01-07")


def test_sim_reads_each_bar_start_and_decides_as_of_the_event() -> None:
    strategy = _Recorder()
    subscriptions = _Runtime(strategy, _mixed_data()).trader._market_data_subscriptions

    ctx = strategy.contexts[1]
    assert ctx.ts == _utc("2026-01-06")
    assert ctx.decision_at == _utc("2026-01-06")
    assert ctx.market_data is not None
    assert ctx.market_data.history(subscriptions["A"]).index[-1] == _utc("2026-01-05")
    assert ctx.market_data.history(subscriptions["B"]).index[-1] == _utc("2026-01-05 14:30")


def test_sim_decides_a_late_bar_with_the_bars_it_delayed(caplog: pytest.LogCaptureFixture) -> None:
    """A's Jan 6 bar is published three days late, with the two bars after
    it: sim fills and decides the three in one event, as a backtest does."""
    data = _mixed_data(["2026-01-06", "2026-01-09", "2026-01-08", "2026-01-09", "2026-01-10"])
    when = lambda ctx: ctx.ts == _utc("2026-01-06 21:00")  # noqa: E731
    backtest_strategy = _Recorder(buy=("A", "B"), when=when)
    result = _run(backtest_strategy, data, execution=_EXECUTION)
    sim_strategy = _Recorder(buy=("A", "B"), when=when)
    polls = [
        _utc(day) + pd.Timedelta(minutes=1) for day in ("2026-01-06", "2026-01-09", "2026-01-10")
    ]
    sim = _Runtime(sim_strategy, data, polls)

    assert _decisions(sim_strategy) == _decisions(backtest_strategy)
    assert (
        sim.entries()
        == _entries(result)
        == {"A": _utc("2026-01-07"), "B": _utc("2026-01-07 14:30")}
    )
    assert "A by up to 1 days" in caplog.text


def test_a_retried_event_takes_the_bars_that_joined_it() -> None:
    """A's late Jan 6 bar is booked, then its decision fails. The retry also
    has A's next two bars, available with it: it books them and decides, as
    a backtest of the same bars does, rather than refusing the event."""
    data = _mixed_data(["2026-01-06", "2026-01-09", "2026-01-08", "2026-01-09", "2026-01-10"])
    when = lambda ctx: ctx.ts == _utc("2026-01-06 21:00")  # noqa: E731
    backtest_strategy = _Recorder(buy=("A", "B"), when=when)
    result = _run(backtest_strategy, data, execution=_EXECUTION)
    failures = [_utc("2026-01-06")]

    def features(frame: pd.DataFrame) -> pd.DataFrame:
        if failures and frame.index[-1] == failures[0]:
            failures.pop()
            raise RuntimeError("feature source down")
        return frame

    sim_strategy = _Recorder(buy=("A", "B"), when=when)
    sim = _Runtime(sim_strategy, data, [_utc("2026-01-06 00:01")], feature_fn=features)
    sim.withheld["A"] = _utc("2026-01-07")
    with pytest.raises(RuntimeError, match="feature source down"):
        sim.poll(_utc("2026-01-09 00:01"))
    sim.withheld.clear()
    for now in (_utc("2026-01-09 00:02"), _utc("2026-01-10 00:01")):
        sim.poll(now)

    assert _decisions(sim_strategy) == _decisions(backtest_strategy)
    assert sim.entries() == _entries(result)


def test_sim_exits_a_breach_on_bars_starting_after_it() -> None:
    """B's Jan 7 close breaches the drawdown limit at 21:00. A's Jan 7 bar
    opened at midnight, before the breach, so A exits on its Jan 8 bar."""
    data = pd.concat(
        [
            _frame("A", _DAYS, [100] * 5),
            _frame("B", [f"{day} 14:30" for day in _DAYS], [100, 100, 10, 10, 10]),
        ]
    )
    when = lambda ctx: ctx.ts == _utc("2026-01-05 21:00")  # noqa: E731
    risk = RiskPolicy(max_drawdown_rate=0.01)
    result = _run(
        _Recorder(buy=("A", "B"), when=when, quantity=100.0), data, execution=_EXECUTION, risk=risk
    )
    sim = _Runtime(_Recorder(buy=("A", "B"), when=when, quantity=100.0), data, risk=risk)

    assert sim.exits() == _exits(result) == {"A": _utc("2026-01-08"), "B": _utc("2026-01-08 14:30")}


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
def test_sim_refuses_legs_on_other_grids_with_the_reason(decision) -> None:
    class _Emit(Strategy):
        def on_bar(self, ctx: Context):
            return decision

    with pytest.raises(ValueError, match="not supported across grids"):
        _Runtime(_Emit(), _mixed_data())


def test_sim_batch_features_on_mixed_grids_see_only_available_bars() -> None:
    seen: list[tuple[pd.Timestamp, pd.Timestamp]] = []

    def features(batch):
        for subscription in batch.primary_subscriptions:
            history = batch.market_data.history(subscription)
            if not history.empty:
                seen.append((pd.Timestamp(batch.as_of), history["available_at"].max()))
        return {
            subscription: batch.market_data.history(subscription)
            for subscription in batch.active_primary_subscriptions
        }

    when = lambda ctx: ctx.bars.get("A", {}).get("close") == 150  # noqa: E731
    result = _run(_Recorder(buy=("A", "B"), when=when), _mixed_data(), execution=_EXECUTION)
    sim = _Runtime(_Recorder(buy=("A", "B"), when=when), _mixed_data(), batch_feature_fn=features)

    assert seen and all(latest <= as_of for as_of, latest in seen)
    assert sim.entries() == _entries(result)
