"""An entries-only pause blocks new exposure while exits keep working.

While paused, an unsubmitted order that opens or adds, or that would cross
its position, is dropped with a ``decision_skipped`` event (reason
``entries_paused``) and every reduce or close still goes out, a flatten's
included. Orders already at the venue and groups already started are left to
finish. The pause is checkpointed, and it is not a halt: an automatic or
manual halt still freezes everything.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from threading import Thread

import pytest
from librae.core.strategy import OrderIntent, PortfolioWeights, Strategy
from librae.live.state import MemoryLiveStateStore

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _HoldStrategy,
    _test_cfg,
    make_live_runner,
    make_live_trader,
    with_run_leases,
)
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _bars,
    _close_all,
    _CountingStrategy,
    _position,
    _submitted,
    _titled,
    _trader,
)
from tests.engine.test_live_operator_halt import (
    PARK_SECONDS,
    _planned_order,
    _resting,
    _run_in_background,
    _single_symbol_trader,
)
from tests.engine.test_live_runner import _broker_report, _mock_order_adapter


class _BuyEveryBar(Strategy):
    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, ctx):
        self.calls += 1
        return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]


def _paused(adapter=None, **kwargs):
    trader, events, alerts = _trader(adapter or _adapter(below_minimum=""), **kwargs)
    trader.pause_entries("desk review")
    return trader, events, alerts


def _skips(events) -> list:
    return [
        event
        for event in events
        if event.event_type == "decision_skipped" and event.detail.get("reason") == "entries_paused"
    ]


def _effects(adapter) -> list[tuple[str, str]]:
    return [
        (call.args[0]["canonical_symbol"], call.args[0]["position_effect"])
        for call in adapter.place_order.call_args_list
    ]


def _working_adapter():
    """Orders rest at the venue until cancelled."""
    adapter = _adapter(below_minimum="")
    adapter.place_order.side_effect = lambda signal: _resting(signal["client_order_id"])
    adapter.get_order.side_effect = lambda order_id, _symbol: _resting(order_id)
    adapter.cancel_order.side_effect = lambda order_id, _symbol: _resting(order_id, "canceled")
    return adapter


def _filling_adapter():
    """Every order fills in full when placed or polled."""
    adapter = _adapter(below_minimum="")
    adapter.get_order.side_effect = lambda order_id, _symbol: _broker_report(
        order_id=order_id, quantity=1.0
    )
    return adapter


def _pair() -> list[OrderIntent]:
    return [
        OrderIntent(action="long", symbol="AAA", quantity=1.0, group_id="pair"),
        OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id="pair"),
    ]


NEXT_BAR = TEST_CLOCK_NOW + timedelta(hours=1)


def _cap_volume(trader, max_rebalance_delay_bars: int = 1) -> None:
    """At a tenth of each bar's volume, so a target may need several bars."""
    trader._max_bar_volume_participation_rate = 0.1
    trader._max_rebalance_delay_bars = max_rebalance_delay_bars


def _thin_aaa_bars() -> dict[str, dict[str, float]]:
    """AAA trades 5 a bar, so the cap lets half of a held 1.0 go per bar."""
    return {**_bars(), "AAA": {"close": 100.0, "volume": 5.0}}


def _failing_persist(*_orders):
    raise OSError("checkpoint store unavailable")


class _FlakyStore(MemoryLiveStateStore):
    """Goes down at the first write ``fails`` matches, or once ``down`` is set.

    ``writes`` keeps every landed write as (snapshot, order rows).
    """

    def __init__(self, fails=lambda _orders: False) -> None:
        super().__init__(restart_durable_for_tests=True)
        self.fails = fails
        self.down = False
        self.writes: list = []

    def save(self, state, orders=()):
        if self.down or self.fails(orders):
            self.down = True
            raise OSError("checkpoint store unavailable")
        super().save(state, orders)
        self.writes.append((deepcopy(state), deepcopy(tuple(orders))))

    def recover(self) -> None:
        self.fails = lambda _orders: False
        self.down = False


class _StoreDownAfterOneSnapshot(MemoryLiveStateStore):
    """Once armed, refuses a write that carries order rows, lands one snapshot, then goes down."""

    def __init__(self) -> None:
        super().__init__(restart_durable_for_tests=True)
        self.armed = False
        self.down = False

    def save(self, state, orders=()):
        if self.armed:
            if self.down or orders:
                raise OSError("checkpoint store unavailable")
            self.down = True
        super().save(state, orders)


class TestPausedOrders:
    @pytest.mark.parametrize(("held", "effect"), [(False, "open"), (True, "add")])
    def test_an_entry_is_dropped_with_its_reason(self, held, effect):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        if not held:
            del trader._positions["BBB"]

        complete = trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BBB", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )

        assert complete is True
        adapter.place_order.assert_not_called()
        assert trader._active_orders == []
        assert trader._halted is False
        [skip] = _skips(events)
        assert skip.symbol == "BBB"
        assert skip.detail["position_effect"] == effect
        assert skip.detail["quantity"] == pytest.approx(1.0)

    def test_reduce_and_close_are_submitted(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)

        complete = trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="AAA"),
                OrderIntent(action="close", symbol="BBB", quantity=0.5),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert _effects(adapter) == [("AAA", "close"), ("BBB", "reduce")]
        assert "AAA" not in trader._positions
        assert trader._positions["BBB"].quantity == pytest.approx(0.5)
        assert _skips(events) == []

    def test_a_close_goes_out_and_the_reopen_behind_it_is_dropped(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="AAA"),
                OrderIntent(action="long", symbol="AAA", quantity=1.0),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert _effects(adapter) == [("AAA", "close")]
        assert "AAA" not in trader._positions
        [skip] = _skips(events)
        assert (skip.symbol, skip.detail["position_effect"]) == ("AAA", "open")

    def test_a_rebalance_sends_its_reductions_then_ends_recording_its_entries(self):
        """AAA flips from short to long: its close goes out, its open does not."""
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        trader._positions = {
            "AAA": replace(_position("AAA"), side="short"),
            "BBB": _position("BBB"),
        }

        complete = trader._execute_live_decision(
            PortfolioWeights({"AAA": 0.5}), _bars(), TEST_CLOCK_NOW
        )

        assert complete is True
        assert sorted(_effects(adapter)) == [("AAA", "close"), ("BBB", "close")]
        assert trader._positions == {}
        assert trader._live_rebalance is None
        [skip] = _skips(events)
        assert skip.detail["symbols"] == ["AAA"]
        assert "target rebalance ended" in skip.detail["message"]

    def test_a_rebalance_works_a_volume_capped_reduction_across_bars_before_its_entry(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        _cap_volume(trader)
        trader._positions = {"AAA": _position("AAA")}

        complete = trader._execute_live_decision(
            PortfolioWeights({"BBB": 0.5}), _thin_aaa_bars(), TEST_CLOCK_NOW
        )

        assert complete is False
        assert trader._positions["AAA"].quantity == pytest.approx(0.5)
        assert trader._live_rebalance is not None
        assert _skips(events) == []

        stopped = trader._continue_live_rebalance(_thin_aaa_bars(), NEXT_BAR)

        assert stopped is False
        assert trader._live_rebalance is None
        assert _effects(adapter) == [("AAA", "reduce"), ("AAA", "close")]
        assert trader._positions == {}
        [skip] = _skips(events)
        assert skip.detail["symbols"] == ["BBB"]

    @pytest.mark.parametrize("entry", ["zero_volume", "below_venue_minimum", "no_bar"])
    def test_a_rebalance_entry_that_could_not_go_out_holds_nothing_up(self, entry):
        adapter = _adapter(below_minimum="BBB" if entry == "below_venue_minimum" else "")
        trader, events, _ = _paused(adapter)
        _cap_volume(trader, max_rebalance_delay_bars=0)
        trader._positions = {"AAA": _position("AAA")}
        bars = _bars()
        if entry == "zero_volume":
            bars["BBB"] = {"close": 100.0, "volume": 0.0}
        elif entry == "no_bar":
            del bars["BBB"]

        complete = trader._execute_live_decision(
            PortfolioWeights({"BBB": 0.5}), bars, TEST_CLOCK_NOW
        )

        assert complete is True
        assert trader._halted is False
        assert _effects(adapter) == [("AAA", "close")]
        assert trader._live_rebalance is None
        [skip] = _skips(events)
        assert skip.detail["symbols"] == ["BBB"]

    def test_a_flip_whose_close_used_the_bar_volume_ends_without_waiting_for_its_open(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        _cap_volume(trader, max_rebalance_delay_bars=0)
        trader._positions = {"AAA": replace(_position("AAA"), side="short")}
        # A tenth of 10 is exactly the short's 1.0, so its close uses the bar up.
        bars = {**_bars(), "AAA": {"close": 100.0, "volume": 10.0}}

        complete = trader._execute_live_decision(
            PortfolioWeights({"AAA": 0.5}), bars, TEST_CLOCK_NOW
        )

        assert complete is True
        assert trader._halted is False
        assert _effects(adapter) == [("AAA", "close")]
        [skip] = _skips(events)
        assert skip.detail["symbols"] == ["AAA"]

    def test_an_entry_queued_before_the_pause_is_dropped_and_the_reductions_carry_on(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _trader(adapter)
        _cap_volume(trader)
        trader._positions = {"AAA": _position("AAA")}
        advance = trader._advance_live_orders

        def advance_until_an_entry(**kwargs):
            head = trader._active_orders[0] if trader._active_orders else None
            if head is None or head.request.position_effect != "open":
                advance(**kwargs)

        # The process stops before sending BBB, queued behind AAA's capped reduction.
        trader._advance_live_orders = advance_until_an_entry
        trader._execute_live_decision(
            PortfolioWeights({"BBB": 0.5}), _thin_aaa_bars(), TEST_CLOCK_NOW
        )
        trader._advance_live_orders = advance
        trader.pause_entries("desk review")

        trader._advance_live_orders()
        stopped = trader._continue_live_rebalance(_thin_aaa_bars(), NEXT_BAR)

        assert stopped is False
        assert trader._live_rebalance is None
        assert _effects(adapter) == [("AAA", "reduce"), ("AAA", "close")]
        assert trader._positions == {}
        dropped, ended = _skips(events)
        assert (dropped.symbol, dropped.detail["position_effect"]) == ("BBB", "open")
        assert ended.detail["symbols"] == ["BBB"]

    def test_a_rebalance_resumed_before_its_reductions_finish_sends_its_entry(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        _cap_volume(trader)
        trader._positions = {"AAA": _position("AAA")}
        trader._execute_live_decision(
            PortfolioWeights({"BBB": 0.5}), _thin_aaa_bars(), TEST_CLOCK_NOW
        )

        trader.resume_entries()
        stopped = trader._continue_live_rebalance(_thin_aaa_bars(), NEXT_BAR)

        assert stopped is False
        assert trader._live_rebalance is None
        assert _effects(adapter) == [("AAA", "reduce"), ("AAA", "close"), ("BBB", "open")]
        assert set(trader._positions) == {"BBB"}
        assert _skips(events) == []

    def test_a_group_with_an_entry_is_dropped_whole_and_a_group_of_exits_goes_out(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        trader._positions = {
            "AAA": replace(_position("AAA"), group_id="pair"),
            "BBB": replace(_position("BBB"), group_id="pair"),
            "DUST": replace(_position("DUST"), group_id="unwind"),
        }

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="AAA", group_id="pair"),
                OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id="pair"),
                OrderIntent(action="close", symbol="DUST", group_id="unwind"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert _effects(adapter) == [("DUST", "close")]
        assert set(trader._positions) == {"AAA", "BBB"}
        skips = _skips(events)
        assert [(skip.symbol, skip.detail["position_effect"]) for skip in skips] == [
            ("AAA", "close"),
            ("BBB", "add"),
        ]
        assert {skip.detail["group_id"] for skip in skips} == {"pair"}

    def test_a_rotation_closes_its_ungrouped_position_and_drops_the_new_entry(self):
        """A close carries its position's group id, so it is not the new group's leg."""
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)
        del trader._positions["BBB"]

        trader._execute_live_decision(
            [
                OrderIntent(action="close", symbol="AAA", group_id="rotate"),
                OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id="rotate"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert _effects(adapter) == [("AAA", "close")]
        [skip] = _skips(events)
        assert (skip.symbol, skip.detail["group_id"]) == ("BBB", "rotate")

    def test_a_close_sized_against_a_dropped_add_is_dropped_too(self):
        """Holding 1, the close was sized at 2 after the add; sent alone it would go short."""
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)

        complete = trader._execute_live_decision(
            [
                OrderIntent(action="long", symbol="BBB", quantity=1.0),
                OrderIntent(action="close", symbol="BBB"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )

        assert complete is True
        adapter.place_order.assert_not_called()
        assert trader._positions["BBB"].quantity == pytest.approx(1.0)
        assert [
            (skip.detail["position_effect"], skip.detail["quantity"]) for skip in _skips(events)
        ] == [("add", pytest.approx(1.0)), ("close", pytest.approx(2.0))]

    def test_an_entry_working_at_the_venue_is_left_alone(self):
        adapter = _working_adapter()
        trader, events, _ = _trader(adapter)
        del trader._positions["BBB"]
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BBB", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )
        [working] = trader._active_orders

        trader.pause_entries("desk review")
        trader._advance_live_orders()

        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == [working]
        assert _skips(events) == []

    def test_a_group_started_before_the_pause_finishes(self):
        adapter = _working_adapter()
        trader, events, _ = _trader(adapter)
        trader._positions = {}
        trader._execute_live_decision(
            [
                OrderIntent(action="long", symbol="AAA", quantity=1.0, group_id="pair"),
                OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id="pair"),
            ],
            _bars(),
            TEST_CLOCK_NOW,
        )
        assert _effects(adapter) == [("AAA", "open")]

        trader.pause_entries("desk review")
        adapter.get_order.side_effect = lambda order_id, _symbol: _broker_report(
            order_id=order_id, quantity=1.0
        )
        adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=signal["client_order_id"], quantity=signal["quantity"]
        )
        trader._advance_live_orders()

        assert _effects(adapter) == [("AAA", "open"), ("BBB", "open")]
        assert set(trader._positions) == {"AAA", "BBB"}
        assert _skips(events) == []

    def test_a_flatten_while_paused_submits_every_exit(self):
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter)

        trader.request_flatten("desk asked to close")
        trader._poll_cycle()

        assert sorted(_submitted(adapter)) == SYMBOLS
        assert trader._positions == {}
        assert trader._halted is True
        assert _skips(events) == []

    def test_the_strategy_keeps_running_while_paused(self):
        events: list = []
        adapter = _mock_order_adapter()
        strategy = _BuyEveryBar()
        trader = make_live_trader(
            strategy,
            adapter,
            on_runtime_event=events.append,
            # The first point at which run() holds the account.
            on_ready=lambda _run_id: trader.pause_entries("desk review"),
        )
        trader._setup_signal_handlers = lambda: None

        trader.run(max_iterations=1)

        assert strategy.calls == 1
        adapter.place_order.assert_not_called()
        [skip] = _skips(events)
        assert skip.detail["position_effect"] == "open"


class TestPauseDurability:
    def test_the_pause_survives_a_restart(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _mock_order_adapter()
        with_run_leases(
            make_live_trader(_HoldStrategy(), adapter, state_store=store)
        ).pause_entries("desk review")
        events: list = []
        strategy = _BuyEveryBar()

        restored = make_live_trader(
            strategy, adapter, state_store=store, on_runtime_event=events.append
        )
        restored._setup_signal_handlers = lambda: None
        restored.run(max_iterations=1)

        [recovered] = [event for event in events if event.event_type == "state_recovered"]
        assert recovered.detail["entries_paused"] is True
        assert strategy.calls == 1
        adapter.place_order.assert_not_called()
        assert len(_skips(events)) == 1

    def _restart(self, store, adapter, events):
        return with_run_leases(
            make_live_trader(
                _HoldStrategy(),
                adapter,
                state_store=store,
                config=_test_cfg(mode="live", symbols=SYMBOLS),
                on_runtime_event=events.append,
            )
        )

    def test_a_restored_group_with_no_leg_sent_is_dropped_whole(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, _, _ = _trader(_adapter(below_minimum=""), state_store=store)
        trader._positions = {}
        trader._advance_live_orders = lambda **_kwargs: None  # the process dies here
        trader._execute_live_decision(_pair(), _bars(), TEST_CLOCK_NOW)
        trader.pause_entries("desk review")
        adapter = _adapter(below_minimum="")
        events: list = []

        restored = self._restart(store, adapter, events)
        restored._advance_live_orders()

        adapter.place_order.assert_not_called()
        assert restored._active_orders == []
        assert [(skip.symbol, skip.detail["group_id"]) for skip in _skips(events)] == [
            ("AAA", "pair"),
            ("BBB", "pair"),
        ]

    def test_a_restored_group_with_a_leg_working_finishes(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, _, _ = _trader(_working_adapter(), state_store=store)
        trader._positions = {}
        trader._execute_live_decision(_pair(), _bars(), TEST_CLOCK_NOW)
        trader.pause_entries("desk review")
        adapter = _filling_adapter()
        events: list = []

        restored = self._restart(store, adapter, events)
        restored._advance_live_orders()

        assert _effects(adapter) == [("BBB", "open")]
        assert set(restored._positions) == {"AAA", "BBB"}
        assert _skips(events) == []

    def test_a_restored_group_whose_sent_leg_completed_finishes(self):
        """Its filled leg left the queue, so only the checkpointed marker shows it started."""
        store = _FlakyStore(
            fails=lambda orders: any(
                order.request.symbol == "BBB" and order.placement_attempted for order in orders
            )
        )
        trader, _, _ = _trader(_filling_adapter(), state_store=store)
        trader._positions = {}
        with pytest.raises(OSError, match="checkpoint store"):
            trader._execute_live_decision(_pair(), _bars(), TEST_CLOCK_NOW)
        store.recover()
        adapter = _filling_adapter()
        events: list = []

        restored = self._restart(store, adapter, events)
        restored.pause_entries("desk review")
        restored._advance_live_orders()

        assert _effects(adapter) == [("BBB", "open")]
        assert set(restored._positions) == {"AAA", "BBB"}
        assert _skips(events) == []

    def test_a_group_queued_while_the_store_is_down_is_still_judged(self):
        store = _FlakyStore()
        adapter = _adapter(below_minimum="")
        trader, events, _ = _paused(adapter, state_store=store)
        trader._positions = {}
        trader._last_reconciliation_at = TEST_CLOCK_NOW
        store.down = True

        with pytest.raises(OSError, match="checkpoint store"):
            trader._execute_live_decision(_pair(), _bars(), TEST_CLOCK_NOW)
        store.recover()
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert trader._active_orders == []
        assert [skip.symbol for skip in _skips(events)] == ["AAA", "BBB"]

    def test_a_group_whose_drop_write_fails_never_sends_its_entry(self):
        store = _FlakyStore(fails=lambda orders: any(o.status == "cancelled" for o in orders))
        adapter = _adapter(below_minimum="")
        trader, _, _ = _paused(adapter, state_store=store)
        trader._positions = {"AAA": replace(_position("AAA"), group_id="pair")}
        trader._last_reconciliation_at = TEST_CLOCK_NOW

        with pytest.raises(OSError, match="checkpoint store"):
            trader._execute_live_decision(
                [
                    OrderIntent(action="close", symbol="AAA", group_id="pair"),
                    OrderIntent(action="long", symbol="BBB", quantity=1.0, group_id="pair"),
                ],
                _bars(),
                TEST_CLOCK_NOW,
            )
        store.recover()
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert trader._active_orders == []
        assert set(trader._positions) == {"AAA"}

    def test_a_dropped_group_leaves_the_queue_in_the_write_that_records_it(self):
        store = _FlakyStore()
        trader, _, _ = _paused(state_store=store)
        trader._positions = {}

        trader._execute_live_decision(_pair(), _bars(), TEST_CLOCK_NOW)

        [(snapshot, rows)] = [
            (state, orders)
            for state, orders in store.writes
            if any(order.status == "cancelled" for order in orders)
        ]
        assert [(row.request.symbol, row.status) for row in rows] == [
            ("AAA", "cancelled"),
            ("BBB", "cancelled"),
        ]
        assert snapshot.active_orders == []
        assert snapshot.unstarted_groups == []

    def test_a_resume_survives_a_restart(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        adapter = _mock_order_adapter()
        trader = with_run_leases(make_live_trader(_HoldStrategy(), adapter, state_store=store))
        trader.pause_entries("desk review")
        trader.resume_entries()

        restored = make_live_trader(_HoldStrategy(), adapter, state_store=store)

        assert restored._entries_paused is False

    def test_a_pause_the_store_refuses_still_applies_and_a_repeat_retries_it(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))
        persist = trader._persist_state
        trader._persist_state = _failing_persist

        with pytest.raises(OSError, match="checkpoint store"):
            trader.pause_entries("desk review")
        assert trader._entries_paused is True
        assert trader._state_store.load(trader._state_key).entries_paused is False

        trader._persist_state = persist
        trader.pause_entries("desk review")

        assert trader._state_store.load(trader._state_key).entries_paused is True
        assert len(_titled(alerts, "Entries Paused")) == 1

    def test_a_resume_the_store_refuses_stays_paused(self):
        trader, _, alerts = _paused()
        trader._persist_state = _failing_persist

        with pytest.raises(OSError, match="checkpoint store"):
            trader.resume_entries()

        assert trader._entries_paused is True
        assert _titled(alerts, "Entries Resumed") == []

    def test_a_resume_whose_order_row_fails_after_the_snapshot_stays_paused_in_memory(self):
        store = _StoreDownAfterOneSnapshot()
        trader, _, _ = _paused(state_store=store)
        carried = _planned_order()
        trader._unpersisted_orders = {carried.request.client_order_id: carried}
        store.armed = True

        with pytest.raises(OSError, match="checkpoint store"):
            trader.resume_entries()

        assert trader._entries_paused is True
        # The accepted window: the store already holds the resume.
        assert store.load(trader._state_key).entries_paused is False


class TestHaltsStayFullFreezes:
    def test_an_ambiguous_placement_while_paused_freezes_the_remaining_exits(self):
        adapter = _adapter(below_minimum="")

        def place(signal):
            if signal["canonical_symbol"] == "AAA":
                raise RuntimeError("connection reset")
            return _broker_report(order_id=signal["client_order_id"], quantity=signal["quantity"])

        adapter.place_order.side_effect = place
        strategy = _CountingStrategy()
        trader, _, alerts = _paused(adapter, strategy=strategy)

        trader._execute_live_decision(_close_all(), _bars(), TEST_CLOCK_NOW)
        trader._poll_cycle()

        assert trader._halted is True
        assert _submitted(adapter) == ["AAA"]
        assert set(trader._positions) == set(SYMBOLS)
        assert [order.request.symbol for order in trader._active_orders] == ["AAA"]
        assert strategy.calls == 0
        assert len(_titled(alerts, "Ambiguous Order Placement")) == 1

    def test_a_halt_while_paused_cancels_the_exit_the_pause_let_through(self):
        adapter = _working_adapter()
        trader, _, _ = _paused(adapter)
        trader._execute_live_decision(
            [OrderIntent(action="close", symbol="AAA")], _bars(), TEST_CLOCK_NOW
        )
        [working] = trader._active_orders
        trader._advance_live_orders()
        adapter.cancel_order.assert_not_called()

        trader.halt("desk asked to stop")

        [cancelled] = adapter.cancel_order.call_args_list
        assert cancelled.args[0] == working.order_id
        assert trader._active_orders == []
        assert trader._halted is True

    def test_reset_halt_leaves_the_pause_in_place(self):
        trader = with_run_leases(_single_symbol_trader(_HoldStrategy(), _mock_order_adapter()))
        trader.pause_entries("desk review")
        trader._halted = True

        trader.reset_halt()

        assert trader._halted is False
        assert trader._entries_paused is True


class TestOperatorCalls:
    def test_resume_lets_entries_out_again(self):
        adapter = _adapter(below_minimum="")
        trader, events, alerts = _paused(adapter)
        del trader._positions["BBB"]

        trader.resume_entries()
        trader._execute_live_decision(
            [OrderIntent(action="long", symbol="BBB", quantity=1.0)], _bars(), TEST_CLOCK_NOW
        )

        assert _effects(adapter) == [("BBB", "open")]
        assert _skips(events) == []
        [paused] = _titled(alerts, "Entries Paused")
        assert "desk review" in paused["message"]
        assert len(_titled(alerts, "Entries Resumed")) == 1

    def test_simulation_refuses_a_pause(self):
        runner = make_live_runner()

        with pytest.raises(RuntimeError, match="live mode"):
            runner.pause_entries("desk review")

        assert runner._entries_paused is False

    def test_pause_rejects_an_empty_reason(self):
        trader, _, _ = _trader(_adapter())

        with pytest.raises(ValueError, match="reason"):
            trader.pause_entries("  ")

    @pytest.mark.parametrize("call", ["pause_entries", "resume_entries"])
    def test_operator_call_from_another_thread_waits_for_the_cycle(self, call):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._entries_paused = call == "resume_entries"
        operator = Thread(target=getattr(trader, call), daemon=True)
        observed: list[tuple[bool, bool]] = []
        fetch = trader._fetch_runtime_frames

        def park_then_fetch():
            if not observed:
                operator.start()
                operator.join(PARK_SECONDS)
                observed.append((operator.is_alive(), trader._entries_paused))
            return fetch()

        trader._fetch_runtime_frames = park_then_fetch
        loop = _run_in_background(trader, iterations=2)
        loop.join(5)
        operator.join(5)

        assert observed == [(True, call == "resume_entries")]
        assert not loop.is_alive() and not operator.is_alive()
        assert trader._entries_paused is (call == "pause_entries")

    @pytest.mark.parametrize("call", ["pause_entries", "resume_entries"])
    def test_a_second_process_cannot_pause_or_resume_the_running_one(self, call):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        running = make_live_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        running._initialize_run()
        running._entries_paused = call == "resume_entries"
        running._persist_state()
        second = make_live_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        alerts: list = []
        second._notify = lambda _method, **kwargs: alerts.append(kwargs)
        stored = store.load(second._state_key)

        try:
            with pytest.raises(RuntimeError, match="not running"):
                getattr(second, call)()
        finally:
            running._release_lease()

        assert second._entries_paused is (call == "resume_entries")
        assert store.load(second._state_key) == stored
        assert alerts == []

    @pytest.mark.parametrize("call", ["pause_entries", "resume_entries"])
    def test_operator_call_after_run_returns_raises_without_mutating(self, call):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        loop = _run_in_background(trader)
        loop.join(5)
        trader._entries_paused = call == "resume_entries"
        persisted: list = []
        trader._persist_state = lambda *orders: persisted.append(orders)

        with pytest.raises(RuntimeError, match="not running"):
            getattr(trader, call)()

        assert trader._entries_paused is (call == "resume_entries")
        assert persisted == []
