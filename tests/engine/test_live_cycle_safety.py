"""Safety checks the live loop keeps running each cycle, and the runs it refuses at startup.

A working order holds strategy decisions for as long as it rests, but not the
drawdown breaker, and a state store outage holds neither the risk checks nor a
flatten's cancels and exits. A cycle that keeps failing with an error that is not an
outage halts instead of retrying unseen. A run whose market data or audit sink
could only fail later is refused at construction, before any fetch.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from librae.core.executor import REASON_OPERATOR_FLATTEN
from librae.core.run_config import RiskPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader
from librae.live.executor import BrokerUnavailableError, OrderRequest
from librae.live.state import MemoryLiveStateStore, StateStoreUnavailableError, TrackedOrder

from tests.engine.live_factories import (
    TEST_CLOCK_NOW,
    _make_ohlcv_df,
    _test_cfg,
    make_live_runner,
    make_live_trader,
    with_run_leases,
)
from tests.engine.test_durable_ohlcv_delivery import _pending
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _accepted,
    _adapter,
    _bars,
    _held_everywhere,
    _position,
    _submitted,
    _trader,
)
from tests.engine.test_live_halt_durability import _resting_adapter
from tests.engine.test_live_runner import _broker_report, _mock_order_adapter

_FIVE_HOURS = timedelta(hours=5)


def _alerts_titled(alerts: list[dict], title: str) -> list[dict]:
    return [alert for alert in alerts if alert["title"].endswith(f"] {title}")]


def _bars_closing_at(close: float):
    """A fetcher whose newest completed bar of every symbol closes at ``close``."""

    def fetch(*_args, **_kwargs):
        frame = _make_ohlcv_df()
        for column in ("open", "high", "low", "close"):
            frame.loc[frame.index[-1], column] = close
        return frame

    return fetch


def _resting_order() -> TrackedOrder:
    return TrackedOrder(
        request=OrderRequest(
            client_order_id="rest-1",
            symbol="AAA",
            side="buy",
            quantity=1.0,
            order_type="limit",
            limit_price=90.0,
            submitted_at=TEST_CLOCK_NOW,
            reference_price=100.0,
        ),
        placement_attempted=True,
        placement_attempted_at=TEST_CLOCK_NOW,
        order_id="rest-1",
        status="accepted",
    )


class _CountingStrategy(Strategy):
    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.calls += 1
        return []


def _resting_trader(close: float, store: MemoryLiveStateStore | None = None, fetcher=None):
    """A trader holding AAA, BBB and DUST at 100 with no cash, a buy resting with no timeout.

    Each poll's newest bar closes at ``close``, unless ``fetcher`` serves the bars.
    """
    adapter = _adapter(below_minimum="")
    adapter.get_order.return_value = _accepted("rest-1")
    adapter.cancel_order.return_value = {**_accepted("rest-1"), "status": "canceled"}
    strategy = _CountingStrategy()
    trader, events, alerts = _trader(
        adapter,
        strategy,
        fetcher=fetcher or _bars_closing_at(close),
        state_store=store or MemoryLiveStateStore(restart_durable_for_tests=True),
    )
    trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
    trader._cash = 0.0
    trader._equity_peak = 300.0
    trader._active_orders = [_resting_order()]
    return trader, adapter, strategy, events, alerts


class TestDrawdownWhileAnOrderRests:
    """The breaker marks the book every poll, whether or not an order is tracked."""

    def test_a_breach_cancels_the_resting_order_then_closes_the_book(self):
        trader, adapter, strategy, events, alerts = _resting_trader(close=50.0)

        trader._poll_cycle()

        assert adapter.cancel_order.call_args.args[0] == "rest-1"
        assert _submitted(adapter) == SYMBOLS
        assert trader._active_orders == []
        assert trader._positions == {}
        assert trader._halted is True
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "drawdown=-50.00% <= -20.00%" in breach["message"]
        assert "flattened account positions and halted" in breach["message"]
        assert [event.detail["reason"] for event in events if event.event_type == "halted"] == [
            "Max Drawdown Breach"
        ]
        assert strategy.calls == 0, "the order still held the decision"

    def test_the_order_keeps_working_and_the_marks_reach_the_checkpoint(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, strategy, _, alerts = _resting_trader(close=110.0, store=store)

        trader._poll_cycle()

        adapter.cancel_order.assert_not_called()
        assert trader._halted is False
        assert [tracked.order_id for tracked in trader._active_orders] == ["rest-1"]
        assert trader._last_prices == {symbol: 110.0 for symbol in SYMBOLS}
        assert trader._equity_peak == pytest.approx(330.0)
        saved = store.load(trader._state_key)
        assert saved.equity_peak == pytest.approx(330.0)
        assert saved.last_prices == {symbol: 110.0 for symbol in SYMBOLS}
        assert trader._last_bar_ts == {}, "the bar stays undecided"
        assert strategy.calls == 0
        assert alerts == []

    def test_a_stale_leg_of_a_hedge_holds_the_check_instead_of_breaching(self):
        """Both legs fell, but only AAA's feed shows it; BBB's short at its old mark would breach."""
        fresh, stale = _bars_closing_at(50.0), _make_ohlcv_df(start_hour=0)

        def fetch(symbol, *_args, **_kwargs):
            return stale if symbol == "BBB" else fresh().assign(ts=lambda f: f["ts"] + _FIVE_HOURS)

        trader, adapter, _, _, alerts = _resting_trader(close=50.0, fetcher=fetch)
        trader.STALE_DATA_TOLERANCE_BARS = 2
        trader._positions = {
            "AAA": _position("AAA"),
            "BBB": replace(_position("BBB"), side="short"),
        }
        trader._equity_peak, _ = trader._calc_account_snapshot()

        trader._poll_cycle()

        assert trader._halted is False
        assert _alerts_titled(alerts, "Max Drawdown Breach") == []
        adapter.cancel_order.assert_not_called()
        assert [tracked.order_id for tracked in trader._active_orders] == ["rest-1"]
        assert len(_alerts_titled(alerts, "Stale Data: BBB")) == 1

    def test_a_requested_flatten_waiting_on_the_order_keeps_the_book(self):
        trader, adapter, _, _, alerts = _resting_trader(close=50.0)
        adapter.cancel_order.return_value = {**_accepted("rest-1"), "status": "pending_cancel"}
        adapter.get_order.return_value = {**_accepted("rest-1"), "status": "pending_cancel"}
        trader.request_flatten("desk asked to close")

        trader._poll_cycle()

        assert trader._halted is False
        assert _alerts_titled(alerts, "Max Drawdown Breach") == []
        adapter.place_order.assert_not_called()


def _store_down(store: MemoryLiveStateStore, *, when=None) -> dict[str, bool]:
    """Fail every save as an outage while ``outage["down"]`` is set, or those ``when`` picks."""
    save = store.save
    outage = {"down": True}

    def guarded_save(state, orders=()):
        if outage["down"] and (when is None or when(state, orders)):
            raise StateStoreUnavailableError("statement timeout")
        save(state, orders)

    store.save = guarded_save
    return outage


def _venue_positions(sizes: dict[str, float]):
    def position(request) -> dict:
        size = sizes.get(request.symbol, 0.0)
        return {"symbol": request.symbol, "size": size, "avg_price": None, "unrealized_pnl": 0.0}

    return position


def _successor(adapter, store: MemoryLiveStateStore, close: float):
    """The process that starts on ``store`` once it answers, restored as its checkpoint says."""
    alerts: list[dict] = []
    trader = with_run_leases(
        make_live_trader(
            _CountingStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=SYMBOLS),
            fetcher=_bars_closing_at(close),
            state_store=store,
        )
    )
    trader._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
    return trader, alerts


def _order(client_order_id: str, symbol: str, **fields) -> TrackedOrder:
    request = OrderRequest(
        client_order_id=client_order_id,
        symbol=symbol,
        side="sell",
        quantity=1.0,
        order_type="market",
        submitted_at=TEST_CLOCK_NOW,
        reference_price=100.0,
        position_effect=fields.pop("position_effect", "reduce"),
        reason=fields.pop("reason", None),
    )
    return TrackedOrder(request=request, **fields)


class TestRiskChecksThroughAStoreOutage:
    """A store outage fails the cycle's writes, not its risk checks nor a flatten.

    The order poll keeps reading and booking, but places nothing. A breach or
    operator flatten sends its cancels and exits with their rows carried in
    memory; a restart before they land halts on the venue's book and sends
    nothing.
    """

    def test_a_breach_cancels_and_closes_at_once_and_is_recorded_once_the_store_answers(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, strategy, events, alerts = _resting_trader(close=50.0, store=store)
        outage = _store_down(store)

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._halted is True
        assert adapter.cancel_order.call_args.args[0] == "rest-1"
        assert _submitted(adapter) == SYMBOLS
        assert trader._positions == {}
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "drawdown=-50.00% <= -20.00%" in breach["message"]
        assert "flattened account positions and halted" in breach["message"]
        assert "not recorded yet (StateStoreUnavailableError" in breach["message"]
        assert trader._unpublished_results, "a fill is published once a checkpoint holds it"

        outage["down"] = False
        trader._poll_cycle()

        saved = store.load(trader._state_key)
        assert saved.halted is True
        assert saved.positions == {}
        assert store.orders["rest-1"].status == "cancelled"
        assert trader._unpublished_results == []
        assert len(_alerts_titled(alerts, "Halt Persisted")) == 1
        assert [event.detail["reason"] for event in events if event.event_type == "halted"] == [
            "Max Drawdown Breach"
        ]
        assert strategy.calls == 0

    def test_exits_waiting_on_a_cancel_go_out_once_the_venue_confirms_it(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=50.0, store=store)
        adapter.cancel_order.return_value = {**_accepted("rest-1"), "status": "pending_cancel"}
        _store_down(store)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        adapter.place_order.assert_not_called()
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "exits wait on the working orders' cancellation" in breach["message"]

        adapter.get_order.return_value = {**_accepted("rest-1"), "status": "canceled"}
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert _submitted(adapter) == SYMBOLS
        assert trader._positions == {}
        assert len(_alerts_titled(alerts, "Halt Cancellation Completed")) == 1

    def test_the_order_poll_keeps_reading_so_a_fill_in_the_outage_counts(self):
        """AAA's buy fills at 90 during the outage: 4 x 81 - 90 is a breach, 3 x 81 is not."""
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, strategy, _, _ = _resting_trader(close=81.0, store=store)
        # Once the order ends no reconciliation round is due, which would read a flat venue.
        trader._last_reconciliation_at = trader._utc_now()
        _store_down(store)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        assert trader._halted is False

        adapter.get_order.return_value = _broker_report(order_id="rest-1", average=90.0)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._halted is True
        assert _submitted(adapter) == SYMBOLS
        assert adapter.place_order.call_args_list[0].args[0]["quantity"] == pytest.approx(2.0)
        assert strategy.calls == 0

    def test_a_fill_in_the_outage_is_checked_against_the_exposure_limits(self):
        """A plain halt still cancels nothing until the store records it."""
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=100.0, store=store)
        trader._risk_policy = RiskPolicy(max_drawdown_rate=0.2, max_position_weight=0.4)
        _store_down(store)
        adapter.get_order.return_value = _broker_report(order_id="rest-1", average=90.0)

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._halted is True
        [breach] = _alerts_titled(alerts, "Post-fill Risk Breach")
        assert "AAA confirmed weight" in breach["message"]
        assert "halt not persisted" in breach["message"]
        adapter.cancel_order.assert_not_called()

    def test_an_operator_flatten_closes_the_book_in_the_outage(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=100.0, store=store)
        trader._active_orders = []
        trader._last_reconciliation_at = trader._utc_now()
        _store_down(store)
        trader.request_flatten("desk asked to close")

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._halted is True
        assert _submitted(adapter) == SYMBOLS
        [flatten] = _alerts_titled(alerts, "Operator Flatten")
        assert "not recorded yet" in flatten["message"]

    def test_a_flatten_exit_queued_when_the_store_goes_down_still_goes_out(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, _ = _resting_trader(close=100.0, store=store)
        working = _order(
            "x-1",
            "AAA",
            position_effect="close",
            reason=REASON_OPERATOR_FLATTEN,
            placement_attempted=True,
            placement_attempted_at=TEST_CLOCK_NOW,
            order_id="x-1",
            status="accepted",
        )
        queued = _order("x-2", "BBB", position_effect="close", reason=REASON_OPERATOR_FLATTEN)
        trader._active_orders = [working, queued]
        trader._halted = True
        trader._persist_state()
        _store_down(store)
        adapter.get_order.return_value = _broker_report(order_id="x-1")

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert _submitted(adapter) == ["BBB"]
        assert sorted(trader._positions) == ["DUST"]

    def test_a_leg_queued_behind_an_order_that_fills_in_the_outage_waits_for_the_store(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, alerts = _resting_trader(close=100.0, store=store)
        trader._last_reconciliation_at = trader._utc_now()
        queued = _order("q-2", "BBB")
        trader._active_orders.append(queued)
        trader._persist_state()
        outage = _store_down(store)
        adapter.get_order.return_value = _broker_report(order_id="rest-1", average=90.0)

        for _ in range(3):
            with pytest.raises(StateStoreUnavailableError):
                trader._poll_cycle()

        assert queued.placement_attempted is False
        adapter.place_order.assert_not_called()
        assert trader._halted is False

        outage["down"] = False
        trader._poll_cycle()

        assert _submitted(adapter) == ["BBB"]
        assert trader._halted is False
        assert _alerts_titled(alerts, "Ambiguous Restored Order") == []

    def test_an_order_whose_placement_write_fails_is_not_taken_as_sent(self):
        adapter = _resting_adapter()
        trader, _, alerts = _trader(adapter)
        outage = _store_down(
            trader._state_store,
            when=lambda _state, orders: any(order.placement_attempted for order in orders),
        )

        with pytest.raises(StateStoreUnavailableError):
            trader._execute_live_decision(
                [OrderIntent(action="long", symbol="AAA", quantity=1.0)], _bars(), TEST_CLOCK_NOW
            )

        [order] = trader._active_orders
        assert order.placement_attempted is False
        assert order.placement_attempted_at is None
        adapter.place_order.assert_not_called()

        outage["down"] = False
        trader._poll_cycle()

        adapter.place_order.assert_called_once()
        assert trader._halted is False
        assert _alerts_titled(alerts, "Ambiguous Restored Order") == []

    def test_an_audit_row_the_sink_takes_in_the_outage_does_not_stop_the_breaker(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, _ = _resting_trader(close=50.0, store=store)
        trader._active_orders = []
        trader._last_reconciliation_at = trader._utc_now()
        taken: list[tuple] = []
        trader._on_ohlcv = lambda *row: taken.append(row)
        trader._pending_ohlcv.append(_pending())
        _store_down(store)

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert taken
        assert trader._halted is True
        assert _submitted(adapter) == SYMBOLS

    @pytest.mark.parametrize(
        ("venue", "halt"),
        [("closed", "Position Reconciliation Mismatch"), ("exit_resting", "Orphan Broker Orders")],
    )
    def test_a_restart_before_the_store_answers_halts_on_the_venue_and_sends_nothing(
        self, venue, halt
    ):
        """The successor knows only the checkpoint from before the breach.

        The venue was closed by exits it does not know, or still holds the
        first of them, which go out one at a time.
        """
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, _ = _resting_trader(close=50.0, store=store)
        trader._persist_state()
        outage = _store_down(store)
        if venue == "exit_resting":
            adapter.place_order.side_effect = lambda signal: _accepted(signal["client_order_id"])
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        sent = [call.args[0]["client_order_id"] for call in adapter.place_order.call_args_list]

        outage["down"] = False
        adapter.get_order.return_value = {**_accepted("rest-1"), "status": "canceled"}
        if venue == "closed":
            assert len(sent) == len(SYMBOLS)
            adapter.get_position.side_effect = _venue_positions({})
        else:
            assert len(sent) == 1
            adapter.get_position.side_effect = _held_everywhere
            adapter.list_open_orders.return_value = [{"id": sent[0], "clientOrderId": sent[0]}]
        adapter.place_order.reset_mock()
        adapter.cancel_order.reset_mock()
        successor, alerts = _successor(adapter, store, close=50.0)
        assert successor._halted is False
        assert successor._pending_flatten is None

        successor._initialize_run()
        successor._poll_cycle()

        assert successor._halted is True
        assert len(_alerts_titled(alerts, halt)) == 1
        adapter.place_order.assert_not_called()
        adapter.cancel_order.assert_not_called()

    def test_a_restart_rereads_a_fill_the_outage_left_unrecorded(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, _ = _resting_trader(close=100.0, store=store)
        trader._last_reconciliation_at = trader._utc_now()
        trader._persist_state()
        outage = _store_down(store)
        adapter.get_order.return_value = _broker_report(order_id="rest-1", average=90.0)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        booked_cash = trader._cash

        outage["down"] = False
        adapter.get_position.side_effect = _venue_positions({"AAA": 2.0, "BBB": 1.0, "DUST": 1.0})
        successor, alerts = _successor(adapter, store, close=100.0)
        assert successor._positions["AAA"].quantity == pytest.approx(1.0), "as recorded"

        successor._initialize_run()

        assert successor._halted is False, alerts
        assert successor._positions["AAA"].quantity == pytest.approx(2.0)
        assert successor._cash == pytest.approx(booked_cash)
        assert successor._active_orders == []


class _RefusedShort(Strategy):
    """Asks every bar for a crypto spot short, which decision preflight refuses."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return [OrderIntent(action="short", symbol=ctx.symbol, quantity=1.0)]


class _FailingAt(Strategy):
    """Raises ``errors[n]`` on its nth call, when that is not None."""

    def __init__(self, *errors: Exception | None) -> None:
        self._errors = list(errors)

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        return []


def _live_runner(strategy: Strategy, **kwargs):
    events: list = []
    alerts: list[dict] = []
    runner = make_live_runner(
        strategy=strategy,
        config=_test_cfg(mode="live"),
        order_adapter=_mock_order_adapter(),
        on_runtime_event=events.append,
        **kwargs,
    )
    runner._notify = lambda method, **kw: alerts.append(kw) if method == "send_alert" else None
    return runner, events, alerts


class TestPersistentPollErrors:
    def test_a_decision_preflight_refuses_on_every_cycle_halts_naming_it(self):
        runner, events, alerts = _live_runner(_RefusedShort())

        runner.run(max_iterations=3)

        assert runner._halted is True
        [halt] = _alerts_titled(alerts, "Persistent Poll Error")
        assert "3 poll cycles in a row failed, the last ValueError:" in halt["message"]
        assert "cannot open or add to a short" in halt["message"]
        assert _alerts_titled(alerts, "Poll Error") == []
        [halted] = [event for event in events if event.event_type == "halted"]
        assert halted.detail["reason"] == "Persistent Poll Error"
        assert "cannot open or add to a short" in halted.detail["message"]

    def test_a_clean_cycle_starts_the_count_over(self):
        error = RuntimeError("feature cache cold")
        strategy = _FailingAt(error, error, None, error, error)
        fetches = {"n": 0}

        def a_new_bar_each_poll(*_args, **_kwargs):
            fetches["n"] += 1
            return _make_ohlcv_df(start_hour=fetches["n"])

        runner, _, alerts = _live_runner(strategy, fetcher=a_new_bar_each_poll)

        runner.run(max_iterations=5)

        assert strategy._errors == [], "every cycle decided a bar"
        assert runner._halted is False
        assert _alerts_titled(alerts, "Persistent Poll Error") == []

    @pytest.mark.parametrize(
        "outage",
        [StateStoreUnavailableError("timeout"), BrokerUnavailableError("venue did not answer")],
    )
    def test_an_outage_keeps_retrying_and_alerts_once(self, outage):
        chained = RuntimeError("AAA order_id=1 status unavailable")
        chained.__cause__ = outage
        runner, _, alerts = _live_runner(_FailingAt(outage, chained, outage, outage))

        runner.run(max_iterations=4)

        assert runner._halted is False
        [poll_error] = _alerts_titled(alerts, "Poll Error")
        assert f"3 consecutive failures, the last {type(outage).__name__}" in poll_error["message"]
        assert _alerts_titled(alerts, "Persistent Poll Error") == []

    def test_a_market_data_fetch_that_keeps_failing_only_alerts(self):
        calls = {"n": 0}

        def fetch(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ConnectionError("feed down")
            return _make_ohlcv_df()

        runner, _, alerts = _live_runner(_CountingStrategy(), fetcher=fetch)

        runner.run(max_iterations=5)

        assert runner._halted is False
        assert len(_alerts_titled(alerts, "Market Data Fetch Failed: BTCUSDT")) == 1
        assert _alerts_titled(alerts, "Persistent Poll Error") == []


class _ShioajiSource:
    """A source declaring the Shioaji route, recording every fetch."""

    market_data_route = "shioaji"

    def __init__(self) -> None:
        self.fetches = 0

    def fetch_ohlcv(self, *_args, **_kwargs):
        self.fetches += 1
        return _make_ohlcv_df()


def _shioaji_cfg(symbol: str, **route):
    return _test_cfg(
        symbols=[symbol],
        market="tw_futures",
        data_source="shioaji",
        instrument_overrides={
            symbol: {
                "data_source": "shioaji",
                "currency": "TWD",
                "calendar_id": "XTAI",
                "venue_symbol": symbol,
                **route,
            }
        },
    )


class TestStartupRefusals:
    @pytest.mark.parametrize(
        "route",
        [
            {"instrument_type": "spot", "security_type": "STK"},
            {"instrument_type": "spot", "security_type": "IND"},
            {"instrument_type": "spot"},
            {
                "instrument_type": "contract_monthly",
                "security_type": "OPT",
                "contract_month": "202612",
            },
        ],
    )
    def test_a_shioaji_poll_of_a_contract_that_is_not_a_future_is_refused(self, route):
        source = _ShioajiSource()

        with pytest.raises(ValueError, match=r"Shioaji market data for \['X'\] cannot be polled"):
            make_live_runner(config=_shioaji_cfg("X", **route), fetcher=source)

        assert source.fetches == 0

    def test_a_shioaji_future_still_starts(self):
        source = _ShioajiSource()
        runner = make_live_runner(
            config=_test_cfg(symbols=["TXFR1"], market="tw_futures", data_source="shioaji"),
            fetcher=source,
        )

        assert isinstance(runner, LiveTrader)

    def test_a_durable_on_ohlcv_refuses_a_batch_path_that_does_not_declare_it(self):
        class _Sink:
            durable_ohlcv_delivery = True

            def on_ohlcv(self, *_args) -> None: ...

            def on_ohlcv_batch(self, *_args) -> None: ...

        sink = _Sink()

        with pytest.raises(ValueError, match="on_ohlcv_batch acknowledges queued rows too"):
            make_live_runner(on_ohlcv=sink.on_ohlcv, on_ohlcv_batch=lambda *args: None)

        assert make_live_runner(on_ohlcv=sink.on_ohlcv, on_ohlcv_batch=sink.on_ohlcv_batch)
