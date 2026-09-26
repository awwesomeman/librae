"""An operator halt must never interleave with the polling loop's own work.

``halt()``, ``reset_halt()`` and ``halt_reset_readiness()`` wait for the cycle
in progress and apply between cycles, so an order the loop is submitting is
either still tracked when the halt cancels it or not yet queued. ``request_halt``
and SIGUSR1 only record the request; the loop applies it before anything else
in its next cycle.
"""

from __future__ import annotations

import signal
from threading import Thread

import pytest
from librae.core.strategy import OrderIntent, Strategy
from librae.live.executor import OrderRequest
from librae.live.state import TrackedOrder

from tests.engine import test_live_runner
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _adapter,
    _CountingStrategy,
    _position,
    _titled,
    _trader,
)
from tests.engine.test_live_runner import TEST_CLOCK_NOW, _HoldStrategy, _mock_order_adapter

PARK_SECONDS = 0.2


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
    trader = test_live_runner.TestLiveExecutionLifecycle()._make_trader(strategy, adapter)
    # run() may only install signal handlers on the main thread.
    trader._setup_signal_handlers = lambda: None
    return trader


def _run_in_background(trader, iterations: int = 1) -> Thread:
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
        loop = _run_in_background(trader)
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
        loop = _run_in_background(trader)
        loop.join(5)
        operator.join(5)

        assert observed == [(True, call != "halt")]
        assert not loop.is_alive() and not operator.is_alive()
        assert trader._halted is (call != "reset_halt")

    def test_a_cycle_waits_for_a_reset_in_progress(self):
        trader = _single_symbol_trader(_HoldStrategy(), _mock_order_adapter())
        trader._halted = True
        loop = Thread(target=trader.run, kwargs={"max_iterations": 1}, daemon=True)
        observed: list[bool] = []
        snapshot = trader._calc_account_snapshot

        def start_cycle_then_snapshot():
            if not observed:
                loop.start()
                loop.join(PARK_SECONDS)
                observed.append(loop.is_alive())
            return snapshot()

        trader._calc_account_snapshot = start_cycle_then_snapshot
        trader.reset_halt()
        loop.join(5)

        assert observed == [True], "a cycle must not start between the readiness check and reset"
        assert not loop.is_alive()
        assert trader._halted is False

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
