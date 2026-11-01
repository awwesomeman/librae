"""An operator halt must never interleave with the polling loop's own work.

``halt()``, ``reset_halt()`` and ``halt_reset_readiness()`` wait for the cycle
in progress and apply between cycles, so an order the loop is submitting is
either still tracked when the halt cancels it or not yet queued. ``request_halt``
and SIGUSR1 only record the request and wake the loop; the loop applies it
before anything else in its next cycle. ``request_halt_reset`` and SIGUSR2 do
the same for a reset, which the loop applies after the halts requested before
it and answers with an alert, since no caller waits for its outcome.
"""

from __future__ import annotations

import os
import signal
import time
from threading import Event, Thread, Timer
from unittest.mock import MagicMock

import pytest
from librae.core.strategy import OrderIntent, Strategy
from librae.live.executor import BrokerUnavailableError, OrderRejectedError, OrderRequest
from librae.live.state import MemoryLiveStateStore, TrackedOrder

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _HoldStrategy,
    _test_cfg,
    make_live_trader,
)
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _CountingStrategy,
    _position,
    _titled,
    _trader,
)
from tests.engine.test_live_runner import _mock_order_adapter

PARK_SECONDS = 0.2


def _wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


class _BuyOnce(Strategy):
    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, ctx):
        self.calls += 1
        if self.calls > 1:
            return []
        return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]


def _resting(order_id: str, status: str = "open") -> dict:
    return {"id": order_id, "status": status, "amount": 1.0, "filled": 0.0}


def _single_symbol_trader(strategy: Strategy, adapter):
    trader = make_live_trader(strategy, adapter)
    # run() may only install signal handlers on the main thread.
    trader._setup_signal_handlers = lambda: None
    return trader


def _run_in_background(trader, iterations: int = 1) -> Thread:
    # A short real sleep between cycles is when a waiting operator call runs.
    trader._sleep = lambda _seconds: time.sleep(PARK_SECONDS)
    loop = Thread(target=trader.run, kwargs={"max_iterations": iterations}, daemon=True)
    loop.start()
    return loop


def _planned_order(symbol: str = "AAA") -> TrackedOrder:
    return TrackedOrder(
        request=OrderRequest(
            client_order_id=f"planned-{symbol}",
            symbol=symbol,
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=TEST_CLOCK_NOW,
            reference_price=100.0,
        )
    )


class TestSynchronousOperatorCalls:
    def test_halt_waits_for_an_order_between_persist_and_submit(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = lambda signal: _resting(signal["client_order_id"])
        adapter.get_order.side_effect = lambda order_id, _symbol: _resting(order_id)
        adapter.cancel_order.side_effect = lambda order_id, _symbol: _resting(order_id, "canceled")
        trader = _single_symbol_trader(_BuyOnce(), adapter)
        operator = Thread(target=trader.halt, args=("desk asked to stop",), daemon=True)
        parked: list[bool] = []
        persist = trader._persist_state

        def persist_then_park(*orders):
            persist(*orders)
            if not parked and any(order.placement_attempted for order in orders):
                operator.start()
                operator.join(PARK_SECONDS)
                parked.append(operator.is_alive())

        trader._persist_state = persist_then_park
        loop = _run_in_background(trader, iterations=2)
        loop.join(5)
        operator.join(5)

        assert parked == [True], "halt() must wait for the submitting cycle"
        assert not loop.is_alive() and not operator.is_alive()
        [placed] = adapter.place_order.call_args_list
        order_id = placed.args[0]["client_order_id"]
        [cancelled] = adapter.cancel_order.call_args_list
        assert cancelled.args[0] == order_id
        assert trader._active_orders == []
        assert trader._halted is True

    @pytest.mark.parametrize("call", ["halt", "reset_halt", "halt_reset_readiness"])
    def test_operator_call_from_another_thread_waits_for_the_cycle(self, call):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._halted = call != "halt"
        operator = Thread(target=getattr(trader, call), daemon=True)
        observed: list[tuple[bool, bool]] = []
        fetch = trader._fetch_runtime_frames

        def park_then_fetch():
            if not observed:
                operator.start()
                operator.join(PARK_SECONDS)
                observed.append((operator.is_alive(), trader._halted))
            return fetch()

        trader._fetch_runtime_frames = park_then_fetch
        loop = _run_in_background(trader, iterations=2)
        loop.join(5)
        operator.join(5)

        assert observed == [(True, call != "halt")]
        assert not loop.is_alive() and not operator.is_alive()
        assert trader._halted is (call != "reset_halt")

    def test_a_cycle_waits_for_a_reset_in_progress(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._halted = True
        cycles: list[bool] = []
        poll = trader._poll_cycle

        def recording_poll():
            cycles.append(trader._halted)
            poll()

        trader._poll_cycle = recording_poll
        observed: list[int] = []
        readiness = trader.halt_reset_readiness

        def ready_then_park():
            answer = readiness()
            # Past the loop's sleep, so its next cycle is due while the reset holds the lock.
            time.sleep(2 * PARK_SECONDS)
            observed.append(len(cycles))
            return answer

        loop = _run_in_background(trader, iterations=2)
        _wait_until(lambda: cycles)
        trader.halt_reset_readiness = ready_then_park
        trader.reset_halt()
        loop.join(5)

        assert observed == [1], "a cycle must not start between the readiness check and reset"
        assert not loop.is_alive()
        assert cycles == [True, False]

    def test_a_blocked_halt_is_recorded_and_the_next_cycle_applies_it(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))
        operator = Thread(target=trader.halt, args=("desk asked to stop",), daemon=True)

        # The loop holds the lock through a hung call, or re-takes it first.
        with trader._cycle_lock:
            operator.start()
            _wait_until(lambda: trader._halt_requests.qsize() == 1)
            assert operator.is_alive()
            assert trader._halted is False
            trader._poll_cycle()
            assert trader._halted is True
        operator.join(5)

        assert not operator.is_alive()
        [halt] = _titled(alerts, "Manual Halt")
        assert "desk asked to stop" in halt["message"]

    def test_a_halt_with_the_lock_free_applies_once_and_synchronously(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))

        trader.halt("desk asked to stop")
        assert trader._halted is True
        trader._poll_cycle()

        assert len(_titled(alerts, "Manual Halt")) == 1

    def test_a_halt_that_fails_raises_to_its_caller(self):
        trader, _, _ = _trader(_adapter(below_minimum=""))

        def failing_persist(*_orders):
            raise OSError("checkpoint store unavailable")

        trader._persist_state = failing_persist

        with pytest.raises(OSError, match="checkpoint store"):
            trader.halt("desk asked to stop")

    def test_the_loop_thread_may_halt_inside_its_own_cycle(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._on_heartbeat = lambda _run_id: trader.halt("heartbeat saw trouble")

        loop = _run_in_background(trader)
        loop.join(5)

        assert not loop.is_alive(), "a halt from inside the cycle must not deadlock"
        assert trader._halted is True


class TestHaltRequest:
    def test_request_from_another_thread_only_records_it(self):
        adapter = _adapter(below_minimum="")
        trader, events, alerts = _trader(adapter)
        trader._active_orders = [_planned_order()]
        persisted: list = []
        trader._persist_state = lambda *orders: persisted.append(orders)

        caller = Thread(target=trader.request_halt, args=("desk asked to stop",))
        caller.start()
        caller.join()

        assert trader._halted is False
        assert len(trader._active_orders) == 1
        adapter.place_order.assert_not_called()
        assert persisted == []
        assert events == []
        assert alerts == []

    def test_request_rejects_an_empty_reason(self):
        trader, _, _ = _trader(_adapter())

        with pytest.raises(ValueError, match="reason"):
            trader.request_halt("  ")

    def test_next_cycle_halts_before_submitting_or_evaluating(self):
        adapter = _adapter(below_minimum="")
        strategy = _CountingStrategy()
        trader, _, alerts = _trader(adapter, strategy=strategy)
        trader._active_orders = [_planned_order()]

        trader.request_halt("desk asked to stop")
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert trader._active_orders == []
        assert trader._halted is True
        assert strategy.calls == 0
        [halt] = _titled(alerts, "Manual Halt")
        assert "desk asked to stop" in halt["message"]

    def test_halt_request_is_applied_before_a_pending_flatten(self):
        adapter = _adapter(below_minimum="")
        trader, _, alerts = _trader(adapter)

        trader.request_flatten("close everything")
        trader.request_halt("stop everything")
        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert set(trader._positions) == set(SYMBOLS)
        assert trader._halted is True
        assert len(_titled(alerts, "Manual Halt")) == 1
        assert len(_titled(alerts, "Operator Flatten Refused")) == 1

    def test_request_arriving_while_one_is_handled_is_kept(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))
        persist = trader._persist_state
        arrived: list[bool] = []

        def persist_and_request(*orders):
            persist(*orders)
            if not arrived:
                arrived.append(True)
                trader.request_halt("second request")

        trader._persist_state = persist_and_request
        trader.request_halt("first request")
        trader._poll_cycle()
        trader._poll_cycle()
        trader._poll_cycle()

        messages = [alert["message"] for alert in _titled(alerts, "Manual Halt")]
        assert len(messages) == 2
        assert "first request" in messages[0]
        assert "second request" in messages[1]

    def test_requests_queued_before_a_cycle_halt_once_with_every_reason(self):
        trader, _, alerts = _trader(_adapter(below_minimum=""))

        trader.request_halt("first request")
        trader.request_halt("second request")
        trader._poll_cycle()
        trader._poll_cycle()

        [halt] = _titled(alerts, "Manual Halt")
        assert "first request" in halt["message"]
        assert "second request" in halt["message"]


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 is POSIX-only")
class TestHaltSignal:
    def test_sigusr1_records_a_halt_request_without_touching_state(self):
        adapter = _adapter(below_minimum="")
        trader, events, alerts = _trader(adapter)
        trader._positions = {"AAA": _position("AAA")}
        trader._active_orders = [_planned_order()]
        persist = trader._persist_state
        persisted: list = []

        def recording_persist(*orders):
            persisted.append(orders)
            persist(*orders)

        trader._persist_state = recording_persist
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1)
        }
        unhandled: list[int] = []
        try:
            # Without the engine's handler, the default action would end the
            # test process instead of failing the test.
            signal.signal(signal.SIGUSR1, lambda signum, _frame: unhandled.append(signum))
            trader._setup_signal_handlers()
            signal.raise_signal(signal.SIGUSR1)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        assert unhandled == []
        assert trader._halted is False
        assert len(trader._active_orders) == 1
        assert persisted == []
        assert events == []
        assert alerts == []

        trader._poll_cycle()

        adapter.place_order.assert_not_called()
        assert trader._halted is True
        [halt] = _titled(alerts, "Manual Halt")
        assert "SIGUSR1" in halt["message"]


class _SignalsHaltMidCycle(Strategy):
    def __init__(self) -> None:
        self.calls = 0
        self.trader = None
        self.halted_during_cycle: bool | None = None

    def on_bar(self, ctx):
        self.calls += 1
        if self.calls == 1:
            signal.raise_signal(signal.SIGUSR1)
            self.halted_during_cycle = self.trader._halted
        return []


def _hourly_trader(strategy: Strategy):
    return make_live_trader(
        strategy,
        _mock_order_adapter(),
        config=_test_cfg(mode="live", poll_seconds=3600),
    )


def _run_with_signal_handlers(trader, iterations: int) -> float:
    """Run on this (main) thread with the engine's handlers; return the seconds taken."""
    previous = {
        signum: signal.getsignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2)
    }
    # A loop that sleeps out poll_seconds is stopped, failing the test
    # instead of hanging it for an hour.
    watchdog = Timer(5.0, trader.stop)
    watchdog.start()
    started = time.monotonic()
    try:
        trader.run(max_iterations=iterations)
    finally:
        watchdog.cancel()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return time.monotonic() - started


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 is POSIX-only")
class TestHaltSignalWakesTheLoop:
    def test_sigusr1_wakes_a_sleeping_loop(self):
        strategy = _CountingStrategy()
        trader = _hourly_trader(strategy)
        sleep = trader._sleep

        def signal_while_sleeping(seconds):
            Timer(0.1, os.kill, args=(os.getpid(), signal.SIGUSR1)).start()
            sleep(seconds)

        trader._sleep = signal_while_sleeping

        elapsed = _run_with_signal_handlers(trader, iterations=2)

        assert trader._halted is True
        assert strategy.calls == 1
        assert elapsed < 5.0

    def test_sigusr1_mid_cycle_is_applied_after_the_cycle_without_waiting(self):
        strategy = _SignalsHaltMidCycle()
        trader = _hourly_trader(strategy)
        strategy.trader = trader

        elapsed = _run_with_signal_handlers(trader, iterations=2)

        assert strategy.halted_during_cycle is False
        assert strategy.calls == 1
        assert trader._halted is True
        assert elapsed < 5.0


class TestHaltOnAHaltedAccount:
    @pytest.mark.parametrize("path", ["halt", "request_halt"])
    def test_flatten_exits_keep_working_through_a_second_halt(self, path):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = lambda signal: _resting(signal["client_order_id"])
        adapter.get_order.side_effect = lambda order_id, _symbol: _resting(order_id)
        trader, _, alerts = _trader(adapter)
        trader.request_flatten("desk asked to close")
        trader._poll_cycle()
        exits = list(trader._active_orders)
        assert trader._halted is True
        assert len(exits) == len(SYMBOLS)

        if path == "halt":
            trader.halt("fail-safe")
        else:
            trader.request_halt("fail-safe")
            trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._active_orders == exits
        assert not any(order.cancel_requested for order in exits)
        [halt] = _titled(alerts, "Manual Halt")
        assert "fail-safe" in halt["message"]
        assert f"already halted; {len(SYMBOLS)} recovery exits keep working" in halt["message"]

    def test_a_refused_exit_after_a_second_halt_is_recorded_not_cancelling_the_rest(self):
        adapter = _adapter(below_minimum="")
        adapter.place_order.side_effect = lambda signal: _resting(signal["client_order_id"])
        trader, events, _ = _trader(adapter)
        trader.request_flatten("desk asked to close")
        trader._poll_cycle()
        refused = trader._active_orders[0]
        adapter.get_order.side_effect = lambda order_id, _symbol: _resting(
            order_id, "rejected" if order_id == refused.order_id else "open"
        )

        trader.halt("fail-safe")
        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._halted is True
        assert refused not in trader._active_orders
        assert len(trader._active_orders) == len(SYMBOLS) - 1
        [skip] = [
            event
            for event in events
            if event.event_type == "decision_skipped"
            and event.detail.get("reason") == "close_rejected"
        ]
        assert skip.symbol == refused.request.symbol


class TestBeforeRun:
    def test_a_halt_while_run_takes_its_leases_waits_and_applies(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        acquire = store.acquire_lease
        acquiring, release = Event(), Event()

        def slow_acquire(key):
            if not acquiring.is_set():
                acquiring.set()
                release.wait(5)
            return acquire(key)

        store.acquire_lease = slow_acquire
        trader = make_live_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        trader._setup_signal_handlers = lambda: None
        errors: list[Exception] = []

        def operator_halt():
            try:
                trader.halt("desk asked to stop")
            except Exception as exc:
                errors.append(exc)

        loop = _run_in_background(trader, iterations=2)
        assert acquiring.wait(5)
        operator = Thread(target=operator_halt, daemon=True)
        operator.start()
        operator.join(PARK_SECONDS)
        assert operator.is_alive(), "halt() must wait for run() to take its leases"
        release.set()
        operator.join(5)
        loop.join(5)

        assert not loop.is_alive() and not operator.is_alive()
        assert errors == []
        assert trader._halted is True

    @pytest.mark.parametrize("call", ["halt", "reset_halt", "halt_reset_readiness"])
    def test_a_second_process_cannot_act_for_the_running_one(self, call):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        running = make_live_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        running._initialize_run()
        running._halted = call != "halt"
        running._persist_state()
        adapter = _mock_order_adapter()
        adapter.get_order.side_effect = lambda order_id, _symbol: _resting(order_id)
        second = make_live_trader(_HoldStrategy(), adapter, state_store=store)
        # As restored from the checkpoint: an order the running process works at the venue.
        resting = _planned_order("BTCUSDT")
        resting.placement_attempted, resting.order_id = True, "venue-1"
        second._active_orders = [resting]
        alerts: list = []
        second._notify = lambda _method, **kwargs: alerts.append(kwargs)
        stored = store.load(second._state_key)

        try:
            with pytest.raises(RuntimeError, match="not running"):
                getattr(second, call)()
        finally:
            running._release_lease()

        assert second._halted is (call != "halt")
        assert store.load(second._state_key) == stored
        adapter.cancel_order.assert_not_called()
        assert alerts == []
        # Only a run of this trader, once it holds the leases, would apply it.
        assert second._halt_requests.qsize() == (1 if call == "halt" else 0)

    def test_a_halt_refused_before_run_keeps_a_requested_one_and_both_apply(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        alerts: list = []
        trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
        trader.request_halt("signalled before the start")

        with pytest.raises(RuntimeError, match="stays queued"):
            trader.halt("desk asked to stop")
        assert trader._halted is False

        trader.run(max_iterations=1)

        assert trader._halted is True
        [halt] = _titled(alerts, "Manual Halt")
        assert "signalled before the start" in halt["message"]
        assert "desk asked to stop" in halt["message"]


def _halted_flat_trader(strategy: Strategy | None = None):
    """A started live trader halted with nothing open, so a reset is allowed."""
    adapter = _mock_order_adapter()
    events: list = []
    alerts: list[dict] = []
    trader = make_live_trader(
        strategy or _HoldStrategy(),
        adapter,
        config=_test_cfg(mode="live", reconciliation_interval_seconds=3600),
        on_runtime_event=events.append,
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._initialize_run()
    trader.halt("operator check")
    events.clear()
    alerts.clear()
    adapter.reset_mock()
    return trader, adapter, events, alerts


def _titles(alerts) -> list[str]:
    return [alert["title"].split("] ", 1)[-1] for alert in alerts]


def _reset_refusals(events) -> list:
    return [
        event
        for event in events
        if event.event_type == "decision_skipped"
        and event.detail.get("reason") == "halt_reset_blocked"
    ]


class TestHaltResetRequest:
    def test_request_from_another_thread_only_records_it(self):
        trader, _, events, alerts = _halted_flat_trader()
        persisted: list = []
        trader._persist_state = lambda *orders: persisted.append(orders)

        caller = Thread(target=trader.request_halt_reset)
        caller.start()
        caller.join()

        assert trader._halted is True
        assert persisted == []
        assert events == []
        assert alerts == []

    def test_next_cycle_resets_and_reconciles_before_any_decision(self):
        strategy = MagicMock(spec=Strategy)
        trader, adapter, _, alerts = _halted_flat_trader(strategy)
        steps: list[str] = []
        flat = adapter.get_position.return_value
        adapter.get_position.side_effect = lambda request: steps.append("reconcile") or flat
        strategy.on_bar.side_effect = lambda ctx: steps.append("decide") or []

        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is False
        assert steps == ["reconcile", "decide"]
        assert _titles(alerts) == ["Halt Reset"]
        assert "reconciles with the broker before any new decision" in alerts[0]["message"]

    def test_an_accepted_reset_holds_decisions_until_the_broker_answers(self):
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        trader, adapter, _, _ = _halted_flat_trader(strategy)
        adapter.get_position.side_effect = BrokerUnavailableError("timeout")

        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is False
        assert trader._resume_unverified is True
        strategy.on_bar.assert_not_called()

    def test_a_refused_reset_alerts_its_reason_and_the_loop_stays_halted(self):
        # Open positions with no bar behind their marks: not resettable.
        trader, events, alerts = _trader(_adapter(below_minimum=""))
        trader.halt("operator check")
        events.clear()
        alerts.clear()

        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused"]
        assert "missing_valuation_mark" in alerts[0]["message"]
        assert "nothing was reset" in alerts[0]["message"]
        [refusal] = _reset_refusals(events)
        assert refusal.detail["cause"] == "missing_valuation_mark"
        assert refusal.detail["blocking_symbols"] == sorted(SYMBOLS)

    def test_an_accepted_reset_leaves_an_entries_pause_in_place(self):
        trader, _, _, alerts = _halted_flat_trader()
        trader.pause_entries("desk wants exits only")
        alerts.clear()

        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is False
        assert trader._entries_paused is True
        assert _titles(alerts) == ["Halt Reset"]

    def test_a_reset_of_a_running_account_changes_nothing(self):
        trader, _, events, alerts = _halted_flat_trader()
        trader.reset_halt()
        trader._poll_cycle()
        assert trader._halted is False
        # A reset would re-base the drawdown peak of a running account.
        trader.reset_halt = MagicMock()

        trader.request_halt_reset()
        trader._poll_cycle()

        trader.reset_halt.assert_not_called()
        assert trader._halted is False
        assert _titles(alerts) == ["Halt Reset Refused"]
        assert "not halted" in alerts[0]["message"]
        assert _reset_refusals(events) == []

    def test_a_reset_whose_checkpoint_fails_still_reports_the_resume(self):
        trader, _, _, alerts = _halted_flat_trader()

        def failing_persist(*_orders):
            raise OSError("checkpoint store unavailable")

        trader._persist_state = failing_persist
        trader.request_halt_reset()

        with pytest.raises(OSError, match="checkpoint store"):
            trader._poll_cycle()

        assert trader._halted is False
        assert _titles(alerts) == ["Halt Reset"]
        assert "a restart now would resume halted" in alerts[0]["message"]

    # RuntimeError included: only the readiness refusal's own subclass is swallowed.
    @pytest.mark.parametrize("error", [OSError, RuntimeError])
    def test_an_unexpected_reset_error_alerts_and_propagates_after_the_halt_retry(self, error):
        trader, _, alerts = _trader(_adapter(below_minimum=""))
        trader.halt("operator check")
        alerts.clear()
        trader._active_orders = [_planned_order()]
        trader._halt_cancels_pending = True

        def failing_sink(_event):
            raise error("event sink unavailable")

        # Fails while recording the refusal, which is not the refusal itself.
        trader._on_runtime_event = failing_sink
        trader.request_halt_reset()

        with pytest.raises(error, match="event sink"):
            trader._poll_cycle()

        assert trader._halted is True
        assert trader._halt_reset_requests.empty()
        # The owed cancellation still ran this cycle.
        assert _titles(alerts) == ["Halt Cancellation Completed", "Halt Reset Refused"]
        assert f"{error.__name__}: event sink unavailable" in alerts[1]["message"]

    def test_a_reset_during_a_store_outage_is_refused_not_held(self):
        trader, _, _, alerts = _halted_flat_trader()
        persist = trader._persist_state

        def failing_persist(*_orders):
            raise OSError("checkpoint store unavailable")

        trader._persist_state = failing_persist
        trader._halt_unpersisted = True
        trader.request_halt_reset()

        with pytest.raises(OSError, match="checkpoint store"):
            trader._poll_cycle()

        assert _titles(alerts) == ["Halt Reset Refused"]
        assert "halt_not_persisted" in alerts[0]["message"]

        trader._persist_state = persist
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused", "Halt Persisted"]


class TestHaltResetOrdering:
    """A reset must never undo a halt requested after it."""

    def test_a_halt_request_in_the_same_cycle_drops_the_reset(self):
        trader, _, _, alerts = _halted_flat_trader()
        trader.reset_halt()

        # Without the drop, the reset would apply right after the halt.
        trader.request_halt_reset()
        trader.request_halt("desk asked to stop")
        trader._poll_cycle()
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused", "Manual Halt"]
        assert "a halt was applied while the reset was pending" in alerts[0]["message"]

    def test_a_synchronous_halt_drops_a_pending_reset(self):
        trader, _, _, alerts = _halted_flat_trader()

        trader.request_halt_reset()
        trader.halt("desk asked again")
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused", "Manual Halt"]

    def test_a_halt_requested_as_the_reset_applies_wins_in_the_same_cycle(self):
        strategy = MagicMock(spec=Strategy)
        trader, adapter, _, alerts = _halted_flat_trader(strategy)
        persist = trader._persist_state
        arrived: list[bool] = []

        def persist_then_request_halt(*orders):
            persist(*orders)
            if not arrived and not trader._halted:
                arrived.append(True)
                trader.request_halt("desk changed its mind")

        trader._persist_state = persist_then_request_halt
        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset", "Manual Halt"]
        strategy.on_bar.assert_not_called()
        adapter.get_position.assert_not_called()

    def test_a_reset_pending_when_an_order_rejection_halts_is_dropped(self):
        trader, adapter, _, alerts = _halted_flat_trader(_BuyOnce())
        trader.reset_halt()

        def request_reset_then_reject(_order):
            # Lands mid-cycle, after this cycle's reset step.
            trader.request_halt_reset()
            raise OrderRejectedError("venue: insufficient margin")

        adapter.place_order.side_effect = request_reset_then_reject
        trader._poll_cycle()
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused", "Order Rejected"]
        assert "a halt was applied while the reset was pending" in alerts[0]["message"]

    def test_a_reset_pending_when_a_flatten_halts_is_dropped(self):
        adapter = _adapter(below_minimum="")
        trader, _, alerts = _trader(adapter)
        report = adapter.place_order.side_effect

        def request_reset_then_fill(order):
            # Lands mid-cycle; the filled exits leave the account resettable.
            trader.request_halt_reset()
            return report(order)

        adapter.place_order.side_effect = request_reset_then_fill
        trader.request_flatten("close everything")
        trader._poll_cycle()
        assert trader._positions == {}
        trader._poll_cycle()

        assert trader._halted is True
        assert _titles(alerts) == ["Halt Reset Refused", "Operator Flatten"]

    def test_a_reset_requested_after_an_applied_halt_resumes(self):
        trader, _, _, alerts = _halted_flat_trader()
        trader.request_halt("desk asked again")
        trader._poll_cycle()

        trader.request_halt_reset()
        trader._poll_cycle()

        assert trader._halted is False
        assert _titles(alerts) == ["Manual Halt", "Halt Reset"]


def _without_cycle_lock():
    class Untakeable:
        def __enter__(self):
            raise AssertionError("took the cycle lock")

        def __exit__(self, *_exc):
            return False

    return Untakeable()


@pytest.mark.skipif(not hasattr(signal, "SIGUSR2"), reason="SIGUSR2 is POSIX-only")
class TestHaltResetSignal:
    def test_sigusr2_records_a_reset_request_without_taking_the_cycle_lock(self):
        trader, _, events, alerts = _halted_flat_trader()
        previous = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGUSR1, signal.SIGUSR2)
        }
        unhandled: list[int] = []
        cycle_lock = trader._cycle_lock
        try:
            signal.signal(signal.SIGUSR2, lambda signum, _frame: unhandled.append(signum))
            trader._setup_signal_handlers()
            # A handler taking the lock could deadlock the loop thread it interrupts.
            trader._cycle_lock = _without_cycle_lock()
            signal.raise_signal(signal.SIGUSR2)
        finally:
            trader._cycle_lock = cycle_lock
            for signum, handler in previous.items():
                signal.signal(signum, handler)

        assert unhandled == []
        assert trader._halted is True
        assert events == []
        assert alerts == []

        trader._poll_cycle()

        assert trader._halted is False
        assert _titles(alerts) == ["Halt Reset"]

    def test_sigusr2_wakes_a_sleeping_loop(self):
        strategy = _CountingStrategy()
        trader = _hourly_trader(strategy)
        trader._halted = True
        sleep = trader._sleep

        def signal_while_sleeping(seconds):
            Timer(0.1, os.kill, args=(os.getpid(), signal.SIGUSR2)).start()
            sleep(seconds)

        trader._sleep = signal_while_sleeping

        elapsed = _run_with_signal_handlers(trader, iterations=2)

        assert trader._halted is False
        assert strategy.calls == 1
        assert elapsed < 5.0


class TestAfterShutdown:
    def test_a_reset_requested_as_the_run_shuts_down_is_dropped(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._halted = True
        trader.reset_halt = MagicMock()
        pool_shutdown = trader._notify_pool.shutdown

        def request_then_shut_down(*args, **kwargs):
            trader.request_halt_reset()
            return pool_shutdown(*args, **kwargs)

        trader._notify_pool.shutdown = request_then_shut_down
        loop = _run_in_background(trader)
        loop.join(5)

        assert not loop.is_alive()
        assert trader._halt_reset_requests.empty()
        trader.reset_halt.assert_not_called()

    def test_a_reset_requested_on_a_closed_trader_does_not_apply_on_a_later_run(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._halted = True
        _run_in_background(trader).join(5)

        trader.request_halt_reset()
        loop = _run_in_background(trader)
        loop.join(5)

        assert not loop.is_alive()
        assert trader._halted is True

    @pytest.mark.parametrize("call", ["halt", "reset_halt", "halt_reset_readiness"])
    def test_operator_call_after_run_returns_raises_without_mutating(self, call):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        loop = _run_in_background(trader)
        loop.join(5)
        trader._halted = call != "halt"
        persisted: list = []
        trader._persist_state = lambda *orders: persisted.append(orders)

        with pytest.raises(RuntimeError, match="not running"):
            getattr(trader, call)()

        assert trader._halted is (call != "halt")
        assert persisted == []
        assert trader._halt_requests.empty()

    def test_a_halt_recorded_as_shutdown_lands_is_not_left_queued(self):
        trader, _, _ = _trader(_adapter(below_minimum=""))
        requests = trader._halt_requests

        class ClosingQueue:
            """Shutdown lands after the caller's first check, before it takes the lock."""

            def put(self, reason):
                requests.put(reason)
                trader._closed = True

            def __getattr__(self, name):
                return getattr(requests, name)

        trader._halt_requests = ClosingQueue()

        with pytest.raises(RuntimeError, match="not running"):
            trader.halt("too late")

        assert requests.empty()
        assert trader._halted is False

    def test_a_halt_waiting_on_shutdown_raises_without_mutating(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        errors: list[str] = []

        def late_halt():
            try:
                trader.halt("too late")
            except RuntimeError as exc:
                errors.append(str(exc))

        operator = Thread(target=late_halt, daemon=True)
        shutting_down: list[bool] = []
        persisted_after: list = []
        persist = trader._persist_state

        def recording_persist(*orders):
            if shutting_down:
                persisted_after.append(orders)
            persist(*orders)

        pool_shutdown = trader._notify_pool.shutdown

        def start_halt_then_shut_down(*args, **kwargs):
            shutting_down.append(True)
            operator.start()
            _wait_until(lambda: not trader._halt_requests.empty())
            operator.join(PARK_SECONDS)
            return pool_shutdown(*args, **kwargs)

        trader._persist_state = recording_persist
        trader._notify_pool.shutdown = start_halt_then_shut_down
        loop = _run_in_background(trader)
        loop.join(5)
        operator.join(5)

        assert not loop.is_alive() and not operator.is_alive()
        assert len(errors) == 1 and "not running" in errors[0]
        assert trader._halted is False
        assert persisted_after == []
        assert trader._halt_requests.empty()
