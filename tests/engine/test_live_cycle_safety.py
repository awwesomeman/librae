"""Safety checks the live loop keeps running each cycle, and the runs it refuses at startup.

A working order holds strategy decisions for as long as it rests, but not the
drawdown breaker, and a state store outage holds neither the breaker nor the
post-fill risk check. A cycle that keeps failing with an error that is not an
outage halts instead of retrying unseen. A run whose market data or audit sink
could only fail later is refused at construction, before any fetch.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
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
)
from tests.engine.test_live_flatten import (
    SYMBOLS,
    _accepted,
    _adapter,
    _held_everywhere,
    _position,
    _submitted,
    _trader,
)
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


def _store_down(store: MemoryLiveStateStore) -> dict[str, bool]:
    """Fail every save as an outage while ``outage["down"]`` is set."""
    save = store.save
    outage = {"down": True}

    def guarded_save(state, orders=()):
        if outage["down"]:
            raise StateStoreUnavailableError("statement timeout")
        save(state, orders)

    store.save = guarded_save
    return outage


class TestRiskChecksThroughAStoreOutage:
    """A store outage fails the order poll's writes, not the checks on what it read.

    Nothing goes to the venue unrecorded, so a halt's cancels and exits wait for
    its checkpoint, and a restart before that finds the venue as recorded.
    """

    def test_a_breach_halts_at_once_and_its_cancel_and_exits_follow_the_store(self):
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, strategy, events, alerts = _resting_trader(close=50.0, store=store)
        outage = _store_down(store)

        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._halted is True
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "drawdown=-50.00% <= -20.00%" in breach["message"]
        assert "exits wait on the working orders' cancellation" in breach["message"]
        assert "halt not persisted (StateStoreUnavailableError" in breach["message"]
        adapter.cancel_order.assert_not_called()
        adapter.place_order.assert_not_called()

        outage["down"] = False
        trader._poll_cycle()

        assert store.load(trader._state_key).halted is True
        assert len(_alerts_titled(alerts, "Halt Persisted")) == 1
        assert adapter.cancel_order.call_args.args[0] == "rest-1"
        assert _submitted(adapter) == SYMBOLS
        assert trader._positions == {}
        assert [event.detail["reason"] for event in events if event.event_type == "halted"] == [
            "Max Drawdown Breach"
        ]
        assert strategy.calls == 0

    def test_the_order_poll_keeps_reading_so_a_fill_in_the_outage_counts(self):
        """AAA's buy fills at 90 during the outage: 4 x 81 - 90 is a breach, 3 x 81 is not."""
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, strategy, _, alerts = _resting_trader(close=81.0, store=store)
        # Once the order ends no reconciliation round is due, which would read a flat venue.
        trader._last_reconciliation_at = trader._utc_now()
        _store_down(store)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        assert trader._halted is False

        adapter.get_order.return_value = _broker_report(order_id="rest-1", average=90.0)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()

        assert trader._positions["AAA"].quantity == pytest.approx(2.0)
        assert trader._halted is True
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "exit submission failed (StateStoreUnavailableError" in breach["message"]
        adapter.place_order.assert_not_called()
        assert strategy.calls == 0

    def test_a_fill_in_the_outage_is_checked_against_the_exposure_limits(self):
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

    def test_a_restart_before_the_store_answers_resumes_as_recorded_and_trips_again(self):
        """The in-memory halt is lost, but nothing reached the venue without a record.

        The successor starts only once the store answers. Startup reads match
        the last checkpoint, so it resumes unhalted with no flatten to resume;
        the first poll measures drawdown from the recorded peak and cancels
        and closes.
        """
        store = MemoryLiveStateStore(restart_durable_for_tests=True)
        trader, adapter, _, _, _ = _resting_trader(close=50.0, store=store)
        # Recorded before the outage: the resting order and the peak.
        trader._persist_state()
        outage = _store_down(store)
        with pytest.raises(StateStoreUnavailableError):
            trader._poll_cycle()
        assert trader._halted is True

        outage["down"] = False
        adapter.get_position.side_effect = _held_everywhere
        restarted, _, alerts = _trader(adapter, fetcher=_bars_closing_at(50.0), state_store=store)
        restarted._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        restarted._initialize_run()

        assert restarted._halted is False
        assert restarted._pending_flatten is None
        [order] = restarted._active_orders
        assert order.cancel_requested is False
        assert restarted._equity_peak == pytest.approx(300.0)
        adapter.cancel_order.assert_not_called()
        adapter.place_order.assert_not_called()

        restarted._poll_cycle()

        assert restarted._halted is True
        assert adapter.cancel_order.call_args.args[0] == "rest-1"
        assert _submitted(adapter) == SYMBOLS
        [breach] = _alerts_titled(alerts, "Max Drawdown Breach")
        assert "flattened account positions and halted" in breach["message"]


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
