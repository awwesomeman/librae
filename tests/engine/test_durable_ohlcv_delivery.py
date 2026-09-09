"""At-least-once delivery for the OHLCV audit trail.

The watermark advances and the checkpoint lands before the audit write is
attempted, so a write that failed was simply lost: a later equal row version
is an idempotent no-op, so nothing re-delivers it. That makes the audit table
quietly diverge from the cache the runtime actually traded on.

Durability here can only mean what the checkpoint can carry. The checkpoint
and the audit rows go to the same database, so recording pending work
anywhere else in that database would be protecting it with the thing that
failed. Riding along with the checkpoint gives the achievable guarantee: the
queue lands atomically with the watermark it belongs to, survives a crash, and
a database that is fully down fails the checkpoint too, so nothing advances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from librae.core.market_data import MarketDataSubscription
from librae.live.state import LiveRuntimeState, PendingOhlcvDelivery


def _subscription(timeframe: str = "H1") -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol="BTCUSDT",
        timeframe=timeframe,
        calendar_id="24/7",
        session_mode="extended",
        data_source="binance_spot",
        instrument_type="spot",
    )


def _pending(ts: str = "2025-01-01T00:00Z", available_at: str = "2025-01-01T01:00Z"):
    return PendingOhlcvDelivery(
        subscription=_subscription(),
        ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        available_at=datetime.fromisoformat(available_at.replace("Z", "+00:00")),
        bar={"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000.0},
    )


def _pending_sequence(count: int) -> list[PendingOhlcvDelivery]:
    """Distinct consecutive bars, so the bound is what the test exercises."""
    base = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        PendingOhlcvDelivery(
            subscription=_subscription(),
            ts=base + timedelta(hours=index),
            available_at=base + timedelta(hours=index + 1),
            bar={"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000.0},
        )
        for index in range(count)
    ]


class TestPendingIdentity:
    def test_the_identity_is_the_subscription_plus_ts_plus_row_version(self) -> None:
        """A correction shares its ts with the bar it replaces, so the row
        version has to be part of what makes two deliveries distinct."""
        original = _pending()
        correction = _pending(available_at="2025-01-01T02:00Z")

        assert original.identity != correction.identity
        assert original.identity == _pending().identity

    def test_the_same_symbol_at_two_frequencies_is_two_identities(self) -> None:
        hourly = _pending()
        daily = PendingOhlcvDelivery(
            subscription=_subscription("D1"),
            ts=hourly.ts,
            available_at=hourly.available_at,
            bar=dict(hourly.bar),
        )

        assert hourly.identity != daily.identity

    def test_it_round_trips_through_the_checkpoint_document(self) -> None:
        restored = PendingOhlcvDelivery.from_dict(_pending().to_dict())

        assert restored == _pending()


class TestTheQueueRidesWithTheCheckpoint:
    @staticmethod
    def _state(pending=()) -> LiveRuntimeState:
        return LiveRuntimeState(
            state_key="sim:abc",
            run_id="r1",
            config_hash="abc",
            mode="sim",
            account_id="default",
            cash=1_000.0,
            pending_ohlcv=list(pending),
        )

    def test_it_survives_a_checkpoint_round_trip(self) -> None:
        state = self._state([_pending()])

        restored = LiveRuntimeState.from_dict(state.to_dict())

        assert restored.pending_ohlcv == [_pending()]

    def test_it_defaults_to_empty(self) -> None:
        assert self._state().pending_ohlcv == []

    def test_ordering_is_preserved_per_subscription(self) -> None:
        """A new bar and a same-ts correction must not be reordered: the
        writer only accepts a strictly later version, so replaying them out of
        order would drop the correction."""
        first = _pending(ts="2025-01-01T00:00Z")
        correction = _pending(ts="2025-01-01T00:00Z", available_at="2025-01-01T02:00Z")
        later = _pending(ts="2025-01-01T01:00Z", available_at="2025-01-01T02:00Z")
        state = self._state([first, correction, later])

        restored = LiveRuntimeState.from_dict(state.to_dict())

        assert [p.identity for p in restored.pending_ohlcv] == [
            first.identity,
            correction.identity,
            later.identity,
        ]


class TestTheQueueIsBounded:
    def test_a_queue_beyond_the_bound_is_rejected(self) -> None:
        """An unbounded queue would grow the checkpoint without limit while
        the database is unavailable, so the bound is part of the contract
        rather than a runtime courtesy."""
        from librae.live.state import MAX_PENDING_OHLCV

        oversized = _pending_sequence(MAX_PENDING_OHLCV + 1)

        with pytest.raises(ValueError, match="pending_ohlcv"):
            LiveRuntimeState(
                state_key="sim:abc",
                run_id="r1",
                config_hash="abc",
                mode="sim",
                account_id="default",
                cash=1_000.0,
                pending_ohlcv=oversized,
            )

    def test_the_bound_itself_is_accepted(self) -> None:
        from librae.live.state import MAX_PENDING_OHLCV

        exactly = _pending_sequence(MAX_PENDING_OHLCV)

        assert (
            len(
                LiveRuntimeState(
                    state_key="sim:abc",
                    run_id="r1",
                    config_hash="abc",
                    mode="sim",
                    account_id="default",
                    cash=1_000.0,
                    pending_ohlcv=exactly,
                ).pending_ohlcv
            )
            == MAX_PENDING_OHLCV
        )


class _DurableSink:
    """A callback that opts into the durable protocol."""

    durable_ohlcv_delivery = True

    def __init__(self) -> None:
        self.accepted: list[tuple] = []
        self.fail = False

    def __call__(self, symbol, timeframe, bar, ts) -> None:
        if self.fail:
            raise RuntimeError("timescale unavailable")
        self.accepted.append((symbol, timeframe, ts))


class _BestEffortSink:
    """A plain user callback, which must keep its existing semantics."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail = False

    def __call__(self, symbol, timeframe, bar, ts) -> None:
        self.calls.append((symbol, timeframe, ts))
        if self.fail:
            raise RuntimeError("user callback exploded")


def _frame(n: int):
    import pandas as pd

    return pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01T00:00Z", periods=n, freq="h", tz="UTC"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": [1_000.0] * n,
        }
    )


def _runner(sink, *, store=None, bars=None):
    from librae.live.state import MemoryLiveStateStore

    from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

    state = bars if bars is not None else {"n": 6}
    return TestLiveTrader()._make_runner(
        config=_test_cfg(mode="sim", warmup_periods=1),
        state_store=store or MemoryLiveStateStore(),
        fetcher=lambda *a, **k: _frame(state["n"]),
        on_ohlcv=sink,
    )


class TestDurableSinkIsRetried:
    def test_a_failed_write_is_retried_on_the_next_cycle(self) -> None:
        sink = _DurableSink()
        bars = {"n": 6}
        runner = _runner(sink, bars=bars)

        sink.fail = True
        runner._poll_cycle()
        assert sink.accepted == []
        assert runner._pending_ohlcv

        sink.fail = False
        bars["n"] = 7
        runner._poll_cycle()

        assert sink.accepted, "the event the failed write dropped must be delivered"

    def test_a_successful_write_leaves_nothing_pending(self) -> None:
        sink = _DurableSink()
        runner = _runner(sink)

        runner._poll_cycle()

        assert sink.accepted
        assert runner._pending_ohlcv == []

    def test_the_pending_queue_lands_in_the_checkpoint(self) -> None:
        """The watermark and the queue must land together, or a crash between
        them loses exactly the row the watermark says was handled."""
        from librae.live.state import MemoryLiveStateStore

        store = MemoryLiveStateStore()
        sink = _DurableSink()
        sink.fail = True
        runner = _runner(sink, store=store)

        runner._poll_cycle()

        saved = store.load(runner._state_key)
        assert saved is not None
        assert saved.pending_ohlcv

    def test_a_restart_replays_the_unacknowledged_event(self) -> None:
        from librae.live.state import MemoryLiveStateStore

        store = MemoryLiveStateStore()
        failing = _DurableSink()
        failing.fail = True
        _runner(failing, store=store)._poll_cycle()

        recovered = _DurableSink()
        restarted = _runner(recovered, store=store, bars={"n": 7})
        assert restarted._restored_state
        restarted._poll_cycle()

        assert recovered.accepted, "a crash before acknowledgement must not lose the row"

    def test_retries_do_not_replay_strategy_decisions(self) -> None:
        """The retry is a persistence concern. Re-running the strategy would
        turn an audit failure into duplicate orders."""
        from librae.core.strategy import Strategy

        evaluated: list = []

        class Count(Strategy):
            def on_bar(self, ctx):
                evaluated.append(ctx.ts)
                return []

        sink = _DurableSink()
        sink.fail = True
        bars = {"n": 6}
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        runner = TestLiveTrader()._make_runner(
            strategy=Count(),
            config=_test_cfg(mode="sim", warmup_periods=1),
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: _frame(bars["n"]),
            on_ohlcv=sink,
        )
        runner._poll_cycle()
        first = list(evaluated)

        sink.fail = False
        runner._poll_cycle()

        assert evaluated == first, "a retry cycle must not re-evaluate an old bar"


class TestBestEffortStaysBestEffort:
    def test_a_plain_callback_failure_is_not_queued(self) -> None:
        """Existing hooks keep their documented semantics: a user callback is
        not a persistence contract, and queueing its failures would make the
        checkpoint grow for something the engine cannot acknowledge."""
        sink = _BestEffortSink()
        sink.fail = True
        runner = _runner(sink)

        runner._poll_cycle()

        assert sink.calls, "the callback is still invoked"
        assert runner._pending_ohlcv == []

    def test_a_plain_callback_failure_does_not_stop_the_cycle(self) -> None:
        sink = _BestEffortSink()
        sink.fail = True
        runner = _runner(sink)

        runner._poll_cycle()

        assert runner._last_bar_ts, "the cycle completed"


class TestOverflowDropsLoudly:
    """Terminal health, not a halt.

    This queue holds OHLCV bars, the most recoverable data in the system: a
    gap is closed by re-fetching from the source, and nothing in the book
    depends on it. Halting cancels live orders and stops trading, which has
    real market cost, to protect data that can be rebuilt. Given that the
    checkpoint and these rows share one database, reaching the bound means the
    database is accepting checkpoints while rejecting OHLCV writes — a schema
    or constraint problem, not an outage.
    """

    @staticmethod
    def _overflowing():
        from librae.live.state import MAX_PENDING_OHLCV

        sink = _DurableSink()
        sink.fail = True
        runner = _runner(sink)
        runner._pending_ohlcv = _pending_sequence(MAX_PENDING_OHLCV)
        return runner

    def test_trading_continues(self) -> None:
        runner = self._overflowing()

        runner._poll_cycle()

        assert not runner._halted

    def test_the_dropped_row_is_recorded(self) -> None:
        """Never silently: the identity that was lost has to be recoverable
        from the audit trail so a backfill knows what to close."""
        runner = self._overflowing()
        events: list = []
        runner._on_runtime_event = events.append

        runner._poll_cycle()

        dropped = [e for e in events if e.detail.get("reason") == "ohlcv_audit_delivery_failed"]
        assert dropped
        assert dropped[0].detail["ts"]
        assert dropped[0].detail["timeframe"]

    def test_health_reports_the_degradation(self) -> None:
        runner = self._overflowing()

        runner._poll_cycle()

        assert runner.ohlcv_audit_degraded is True

    def test_the_alert_fires_once_not_per_row(self) -> None:
        runner = self._overflowing()
        alerts: list = []
        runner._notify = lambda method, **kwargs: alerts.append(kwargs)

        runner._poll_cycle()
        runner._poll_cycle()

        titles = [a.get("title", "") for a in alerts]
        assert sum("Audit Backlog" in title for title in titles) == 1

    def test_recovery_clears_the_degraded_flag(self) -> None:
        runner = self._overflowing()
        runner._poll_cycle()
        assert runner.ohlcv_audit_degraded is True

        runner._on_ohlcv.fail = False
        runner._poll_cycle()

        assert runner.ohlcv_audit_degraded is False


def test_the_reference_persistence_path_opts_in() -> None:
    """The engine reads the declaration off the callback's owner, so a bound
    method of the Timescale callbacks carries it."""
    from librae.orchestration.live import _TimescaleCallbacks

    assert _TimescaleCallbacks.durable_ohlcv_delivery is True


class TestTheReferencePathActuallyRetries:
    """The declaration is not the contract; propagating failure is.

    The first-party callback routed its write through a wrapper that logs and
    swallows every exception, so it returned normally after a failed write.
    The engine read that as acknowledgement and dropped the row — the durable
    protocol was declared and never engaged, and the only observable effect
    was extra checkpoint writes.
    """

    @staticmethod
    def _callbacks():
        from librae.config.symbols import resolve_symbol
        from librae.orchestration.live import _TimescaleCallbacks

        from tests.engine.test_live_runner import _test_cfg

        config = _test_cfg(mode="sim", warmup_periods=1)
        callbacks = _TimescaleCallbacks(
            config, {"BTCUSDT": resolve_symbol(config, "BTCUSDT")}, None
        )
        callbacks._run_id = "r1"
        return config, callbacks

    def test_a_failed_write_propagates_out_of_the_reference_callback(self) -> None:
        from unittest.mock import patch

        def boom(*_args, **_kwargs):
            raise RuntimeError("timescale unavailable")

        _, callbacks = self._callbacks()
        bar = {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1_000.0}

        with (
            patch("librae.db.timescale_writer.write_ohlcv", boom),
            pytest.raises(RuntimeError, match="timescale unavailable"),
        ):
            callbacks.on_ohlcv("BTCUSDT", "H1", bar, datetime(2025, 1, 1, tzinfo=UTC))

    def test_other_reference_writes_stay_best_effort(self) -> None:
        """Only the durable path changes. A heartbeat is a recoverable
        next-bar snapshot and must keep swallowing."""
        from unittest.mock import patch

        def boom(*_args, **_kwargs):
            raise RuntimeError("timescale unavailable")

        _, callbacks = self._callbacks()

        with patch("librae.db.timescale_writer.update_heartbeat", boom):
            callbacks.on_heartbeat("r1")

    def test_the_engine_replays_through_the_real_reference_callback(self) -> None:
        """End to end on the path the feature exists for, rather than a test
        double that raises where the real one does not."""
        from unittest.mock import patch

        from librae.live.state import MemoryLiveStateStore

        from tests.engine.test_live_runner import TestLiveTrader

        config, callbacks = self._callbacks()
        accepted: list = []

        def record(df, subscription, *_args, **_kwargs):
            accepted.append((subscription.symbol, len(df)))
            return len(df)

        def boom(*_args, **_kwargs):
            raise RuntimeError("timescale unavailable")

        bars = {"n": 6}
        runner = TestLiveTrader()._make_runner(
            config=config,
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: _frame(bars["n"]),
            on_ohlcv=callbacks.on_ohlcv,
        )

        with patch("librae.db.timescale_writer.write_ohlcv", boom):
            runner._poll_cycle()
        assert runner._pending_ohlcv, "a failed reference write must stay pending"

        bars["n"] = 7
        with patch("librae.db.timescale_writer.write_ohlcv", record):
            runner._poll_cycle()

        assert accepted, "the row the failed write dropped must reach the writer"
        assert runner._pending_ohlcv == []


class TestTheCheckpointRecordsWhatTheSinkAccepted:
    """`flush_pending` truncates the queue before it persists.

    Asserted on the queue as `persist` saw it, not on the list afterwards:
    both orderings leave the same list behind, which is why swapping the two
    statements leaves the rest of the suite green. Persisting first records
    rows the sink already took, so a crash before the next checkpoint
    restarts with them still queued — harmless to redeliver, but they hold
    headroom against MAX_PENDING_OHLCV, whose overflow now drops rows.
    """

    @staticmethod
    def _flush(pending, sink):
        """Return the queue as the checkpoint saw it, or None if never written."""
        from librae.live.ohlcv_audit import flush_pending

        checkpointed: list[list[object]] = []
        flush_pending(
            pending,
            on_ohlcv=sink,
            degraded=False,
            persist=lambda: checkpointed.append([row.identity for row in pending]),
        )
        return checkpointed[-1] if checkpointed else None

    def test_a_full_drain_checkpoints_an_empty_queue(self) -> None:
        pending = _pending_sequence(3)

        checkpointed = self._flush(pending, lambda *args: None)

        assert checkpointed == []

    def test_a_partial_drain_checkpoints_exactly_the_undelivered_tail(self) -> None:
        pending = _pending_sequence(3)
        expected_tail = [row.identity for row in pending[1:]]
        accepted = 0

        def sink(*args: object) -> None:
            nonlocal accepted
            if accepted == 1:
                raise RuntimeError("sink is down")
            accepted += 1

        checkpointed = self._flush(pending, sink)

        assert accepted == 1
        # Order matters: the writer accepts only a strictly later row version,
        # so replaying a correction before the bar it corrects would drop it.
        assert checkpointed == expected_tail

    def test_a_sink_that_takes_nothing_writes_no_checkpoint(self) -> None:
        pending = _pending_sequence(2)
        untouched = [row.identity for row in pending]

        def sink(*args: object) -> None:
            raise RuntimeError("sink is down")

        assert self._flush(pending, sink) is None
        assert [row.identity for row in pending] == untouched
