"""A ``[close, opposite-side entry]`` pair reverses a position in one decision.

The pair takes the reduce-then-add path a PortfolioWeights flip takes: a
backtest and a sim fill both legs at one open, and live sends the entry only
once the close has filled. Every other repeat of a symbol in a decision is
still refused. An entry that cannot fill in full leaves the position flat or
partial, never larger than asked, and a halt before the entry goes out drops
it. In a backtest or sim the legs fill in one step, so a halt can only come
before both of them.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    REASON_FORCE_CLOSE,
    validate_strategy_decision,
)
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState, Strategy

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _test_cfg,
    make_live_runner,
    make_live_trader,
    with_run_leases,
)
from tests.engine.test_live_operator_halt import _resting
from tests.engine.test_live_runner import _broker_report, _mock_order_adapter

SYMBOL = "AAA"
HOURS = pd.date_range("2025-01-01", periods=10, freq="h", tz="UTC")
# Decided on these bars, filled at the next bar's open.
LONG_AT, REVERSE_AT = HOURS[4], HOURS[6]
FILLED_AT = HOURS[7]


def _reversal(entry: str = "short", **entry_fields) -> list[OrderIntent]:
    return [
        OrderIntent(action="close", symbol=SYMBOL),
        OrderIntent(action=entry, symbol=SYMBOL, quantity=1.0, **entry_fields),
    ]


class _LongThenReverse(Strategy):
    def __init__(self, quantity: float = 1.0, *, weights: bool = False) -> None:
        self._quantity = quantity
        self._weights = weights

    def on_bar(self, ctx):
        if ctx.ts == LONG_AT:
            return [OrderIntent(action="long", symbol=SYMBOL, quantity=self._quantity)]
        if ctx.ts == REVERSE_AT:
            return PortfolioWeights({SYMBOL: -0.001}) if self._weights else _reversal()
        return []


def _bars(*, crash: bool = False) -> pd.DataFrame:
    opens = 100.0 + np.arange(10.0)
    closes = opens + 0.5
    if crash:
        # REVERSE_AT closes far below the long's entry, and FILLED_AT opens there.
        closes[6] = 50.0
        opens[7] = 50.0
    return pd.DataFrame(
        {
            "ts": HOURS,
            "open": opens,
            "high": np.maximum(opens, closes) + 1.0,
            "low": np.minimum(opens, closes) - 1.0,
            "close": closes,
            "volume": 1000.0,
        }
    )


def _config(mode: str, **overrides):
    # Equity spot: shortable, and a backtest needs no funding data for it.
    return _test_cfg(
        mode=mode,
        symbols=[SYMBOL],
        instrument_overrides={SYMBOL: {"market": "us_equity", "currency": "USDT"}},
        symbol_cost_overrides={SYMBOL: {"multiplier": 1.0}},
        **overrides,
    )


def _fills(events) -> list[tuple]:
    return [
        (pd.Timestamp(event.ts), event.event_type, event.side, event.fill_quantity, event.price)
        for event in events
    ]


def _backtest(strategy: Strategy, frame: pd.DataFrame, **config):
    data = frame.drop(columns="ts")
    data.index = pd.MultiIndex.from_arrays(
        [[SYMBOL] * len(frame), frame["ts"]], names=["symbol", "datetime"]
    )
    backtest = Backtest(
        data, strategy, config=_config("backtest", **config), cost_model=CostModel.zero()
    )
    backtest.run()
    output = backtest.build_output()
    # The run's end closes what is still open; a sim has no end to close at.
    events = [event for event in output.position_events if event.reason != REASON_FORCE_CLOSE]
    return events, list(output.runtime_events)


def _sim(strategy: Strategy, frame: pd.DataFrame, **config):
    # Each poll sees one more closed bar, from the fifth (LONG_AT) to the ninth.
    windows = iter([frame.iloc[: end + 1] for end in range(4, 9)])
    runner = make_live_runner(
        strategy=strategy,
        fetcher=lambda *_args, **_kwargs: next(windows),
        config=_config("sim", **config),
    )
    position_events: list = []
    runtime_events: list = []
    runner._on_position_event = lambda event, _sequence: position_events.append(event)
    runner._on_runtime_event = runtime_events.append
    runner.run(max_iterations=5)
    return position_events, runtime_events


def _volume_capped(rate: float) -> dict:
    """A bar's fills of the symbol may together take ``rate`` of the decision bar's 1000."""
    return {"execution": ExecutionPolicy(max_bar_volume_participation_rate=rate, warmup_periods=5)}


class TestSimulatedReversal:
    @pytest.mark.parametrize("run", [_backtest, _sim], ids=["backtest", "sim"])
    def test_both_legs_fill_at_the_next_open(self, run):
        events, _ = run(_LongThenReverse(), _bars())

        assert _fills(events) == [
            (HOURS[5], "open", "long", 1.0, 105.0),
            (FILLED_AT, "close", "long", 1.0, 107.0),
            (FILLED_AT, "open", "short", 1.0, 107.0),
        ]

    def test_the_pair_fills_where_a_weights_flip_fills(self):
        pair, _ = _backtest(_LongThenReverse(), _bars())
        weights, _ = _backtest(_LongThenReverse(weights=True), _bars())

        def legs(events):
            return [(ts, kind, side, price) for ts, kind, side, _qty, price in _fills(events)[1:]]

        assert legs(pair) == legs(weights)

    @pytest.mark.parametrize("run", [_backtest, _sim], ids=["backtest", "sim"])
    def test_an_entry_the_close_leaves_short_of_volume_fills_partly(self, run):
        events, _ = run(_LongThenReverse(), _bars(), **_volume_capped(0.0015))

        assert _fills(events)[1:] == [
            (FILLED_AT, "close", "long", 1.0, 107.0),
            (FILLED_AT, "open", "short", pytest.approx(0.5), 107.0),
        ]

    @pytest.mark.parametrize("run", [_backtest, _sim], ids=["backtest", "sim"])
    def test_an_entry_with_no_volume_left_is_refused_and_the_book_ends_flat(self, run):
        events, runtime_events = run(_LongThenReverse(), _bars(), **_volume_capped(0.001))

        assert _fills(events)[1:] == [(FILLED_AT, "close", "long", 1.0, 107.0)]
        assert [
            event.detail["reason"]
            for event in runtime_events
            if event.event_type == "decision_skipped" and pd.Timestamp(event.ts) == FILLED_AT
        ] == ["volume_capped"]

    @pytest.mark.parametrize("run", [_backtest, _sim], ids=["backtest", "sim"])
    def test_a_halt_on_the_deciding_bar_drops_both_legs(self, run):
        events, _ = run(
            _LongThenReverse(quantity=900.0),
            _bars(crash=True),
            risk=RiskPolicy(max_drawdown_rate=0.1),
        )

        # The drawdown exit closes the long; no short opens.
        assert [(pd.Timestamp(event.ts), event.event_type, event.reason) for event in events] == [
            (HOURS[5], "open", ""),
            (FILLED_AT, "close", REASON_DRAWDOWN_BREACH),
        ]


def _held(side: str = "long", quantity: float = 1.0) -> dict[str, PositionState]:
    return {
        SYMBOL: PositionState(
            symbol=SYMBOL,
            side=side,
            entry_price=100.0,
            quantity=quantity,
            entry_at=HOURS[0],
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=100.0 * quantity,
        )
    }


def _validate(decision, positions) -> None:
    validate_strategy_decision(
        decision,
        {SYMBOL},
        primary_symbol=SYMBOL,
        bars={SYMBOL: {"open": 100.0, "close": 100.0, "volume": 1000.0}},
        positions=positions,
    )


class TestAcceptedShape:
    @pytest.mark.parametrize(
        ("decision", "positions"),
        [
            (_reversal("short"), _held("long")),
            (_reversal("long"), _held("short")),
            (
                [
                    OrderIntent(action="close", symbol=SYMBOL, quantity=1.0),
                    OrderIntent(action="short", symbol=SYMBOL),
                ],
                _held("long"),
            ),
            (
                [
                    OrderIntent(action="close", symbol=SYMBOL, time_in_force="ioc"),
                    OrderIntent(action="short", symbol=SYMBOL, quantity=1.0, time_in_force="fok"),
                ],
                _held("long"),
            ),
        ],
        ids=["long-to-short", "short-to-long", "explicit-whole-close", "ioc-fok"],
    )
    def test_a_reversal_of_the_held_position_is_accepted(self, decision, positions):
        _validate(decision, positions)

    @pytest.mark.parametrize(
        ("decision", "positions"),
        [
            (list(reversed(_reversal())), _held("long")),
            (_reversal(), {}),
            (_reversal("long"), _held("long")),
            (
                [
                    OrderIntent(action="close", symbol=SYMBOL, quantity=0.5),
                    OrderIntent(action="short", symbol=SYMBOL, quantity=1.0),
                ],
                _held("long"),
            ),
            (
                [replace(intent, group_id="flip") for intent in _reversal()],
                _held("long"),
            ),
            (_reversal(limit_price=99.0), _held("long")),
            (_reversal(time_in_force="gtc"), _held("long")),
            (
                [OrderIntent(action="close", symbol=SYMBOL)] * 2,
                _held("long"),
            ),
            ([*_reversal(), OrderIntent(action="short", symbol=SYMBOL, quantity=1.0)], _held()),
        ],
        ids=[
            "entry-first",
            "nothing-held",
            "same-side",
            "partial-close",
            "grouped",
            "limit-entry",
            "resting-entry",
            "two-closes",
            "three-intents",
        ],
    )
    def test_every_other_repeat_of_a_symbol_is_refused(self, decision, positions):
        with pytest.raises(ValueError, match="at most one intent per symbol"):
            _validate(decision, positions)


WORKING = {"status": "open", "amount": 1.0, "filled": 0.0}
REJECTED = {"status": "rejected", "amount": 1.0, "filled": 0.0}
PART_FILLED = {**_broker_report(quantity=1.0, filled=0.4), "status": "partially_filled"}
PART_FILLED_CANCELLED = {**PART_FILLED, "status": "canceled"}


def _live_adapter(
    *, close: dict | None = None, entry: dict | None = None, polled: dict | None = None
):
    """Fills every order when placed, unless ``close`` or ``entry`` is that
    leg's answer; a status read answers ``polled``, or a fill."""
    adapter = _mock_order_adapter()
    answers = {"close": close, "open": entry}

    def place(signal):
        order_id = signal["client_order_id"]
        answer = answers.get(signal["position_effect"])
        if answer is None:
            return _broker_report(order_id=order_id, quantity=signal["quantity"])
        return {**answer, "id": order_id}

    adapter.place_order.side_effect = place
    adapter.get_order.side_effect = lambda order_id, _symbol: (
        {**polled, "id": order_id} if polled is not None else _broker_report(order_id=order_id)
    )
    adapter.cancel_order.side_effect = lambda order_id, _symbol: _resting(order_id, "canceled")
    return adapter


def _live_trader(adapter):
    events: list = []
    trader = with_run_leases(
        make_live_trader(
            _LongThenReverse(), adapter, config=_config("live"), on_runtime_event=events.append
        )
    )
    trader._notify = lambda *_args, **_kwargs: None
    trader._positions = _held("long")
    trader._last_prices = {SYMBOL: 100.0}
    return trader, events


def _decide(trader, decision) -> bool:
    """Validate and execute ``decision`` as the live cycle does."""
    bars = {SYMBOL: {"close": 100.0, "volume": 10_000.0}}
    validate_strategy_decision(
        decision,
        {SYMBOL},
        primary_symbol=SYMBOL,
        bars=bars,
        positions=trader._positions,
        can_short=trader._can_short,
    )
    return trader._execute_live_decision(decision, bars, TEST_CLOCK_NOW)


def _placed(adapter) -> list[tuple[str, str]]:
    return [
        (call.args[0]["side"], call.args[0]["position_effect"])
        for call in adapter.place_order.call_args_list
    ]


class TestLiveReversal:
    def test_the_entry_goes_out_only_after_the_close_has_filled(self):
        adapter = _live_adapter(close=WORKING)
        trader, _ = _live_trader(adapter)

        assert _decide(trader, _reversal()) is False
        assert _placed(adapter) == [("sell", "close")]
        assert [order.request.position_effect for order in trader._active_orders] == [
            "close",
            "open",
        ]

        trader._advance_live_orders()

        assert _placed(adapter) == [("sell", "close"), ("sell", "open")]
        assert trader._active_orders == []
        assert trader._halted is False
        position = trader._positions[SYMBOL]
        assert (position.side, position.quantity) == ("short", 1.0)

    def test_a_rejected_entry_leaves_the_book_flat_and_halts(self):
        adapter = _live_adapter(entry=REJECTED)
        trader, _ = _live_trader(adapter)

        _decide(trader, _reversal())

        assert _placed(adapter) == [("sell", "close"), ("sell", "open")]
        assert trader._positions == {}
        assert trader._halted is True

    def test_a_partly_filled_entry_leaves_a_smaller_short_and_halts(self):
        adapter = _live_adapter(entry=PART_FILLED_CANCELLED)
        trader, _ = _live_trader(adapter)

        _decide(trader, _reversal())

        position = trader._positions[SYMBOL]
        assert (position.side, position.quantity) == ("short", pytest.approx(0.4))
        assert trader._halted is True

    def test_a_halt_between_the_legs_cancels_the_entry(self):
        adapter = _live_adapter(close=WORKING, polled=WORKING)
        trader, _ = _live_trader(adapter)
        _decide(trader, _reversal())

        trader.halt("desk asked to stop")

        assert _placed(adapter) == [("sell", "close")]
        assert len(adapter.cancel_order.call_args_list) == 1
        assert trader._active_orders == []
        assert trader._halted is True
        # The close was cancelled unfilled, so the long is still held.
        assert trader._positions[SYMBOL].side == "long"

    @pytest.mark.parametrize("group_id", [None, "pair"], ids=["ungrouped", "grouped"])
    @pytest.mark.parametrize(
        ("placed", "polled", "left"),
        [
            (REJECTED, None, 1.0),
            ({**REJECTED, "status": "expired"}, None, 1.0),
            (PART_FILLED, PART_FILLED_CANCELLED, 0.6),
        ],
        ids=["rejected", "expired", "partly-filled-then-cancelled"],
    )
    def test_a_close_that_does_not_fill_halts_and_never_sends_the_entry(
        self, placed, polled, left, group_id
    ):
        """A grouped position's close fails only its group, so the entry needs its own guard."""
        adapter = _live_adapter(close=placed, polled=polled)
        trader, _ = _live_trader(adapter)
        trader._positions = {SYMBOL: replace(_held()[SYMBOL], group_id=group_id)}

        _decide(trader, _reversal())
        # A close still working when placed ends on the next status read.
        trader._advance_live_orders()

        assert _placed(adapter) == [("sell", "close")]
        assert trader._active_orders == []
        assert trader._halted is True
        position = trader._positions[SYMBOL]
        assert (position.side, position.quantity) == ("long", pytest.approx(left))


class _ShortWhileLong(Strategy):
    """Goes long, then asks for a short without closing on every later bar."""

    def on_bar(self, ctx):
        if SYMBOL not in ctx.positions:
            return [OrderIntent(action="long", symbol=SYMBOL, quantity=1.0)]
        return [OrderIntent(action="short", symbol=SYMBOL, quantity=1.0)]


def _refusal_warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "opposite its open position" in record.message
    ]


class TestOppositeSideRefusalLog:
    @pytest.mark.parametrize("run", [_backtest, _sim], ids=["backtest", "sim"])
    def test_a_repeated_refusal_is_logged_once_per_run(self, run, caplog):
        caplog.set_level(logging.WARNING)

        _, runtime_events = run(_ShortWhileLong(), _bars())

        refusals = [
            event for event in runtime_events if event.detail.get("reason") == "opposite_side"
        ]
        assert len(refusals) > 1
        [warning] = _refusal_warnings(caplog)
        assert SYMBOL in warning.message

    def test_live_logs_a_repeated_refusal_once(self, caplog):
        caplog.set_level(logging.WARNING)
        trader, events = _live_trader(_live_adapter())
        bare_short = [OrderIntent(action="short", symbol=SYMBOL, quantity=1.0)]

        for _ in range(3):
            trader._execute_live_decision(
                bare_short, {SYMBOL: {"close": 100.0, "volume": 10_000.0}}, TEST_CLOCK_NOW
            )

        assert [event.detail["reason"] for event in events] == ["opposite_side"] * 3
        assert len(_refusal_warnings(caplog)) == 1
