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

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pandas as pd
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
    available = datetime.fromisoformat(available_at.replace("Z", "+00:00"))
    return PendingOhlcvDelivery(
        subscription=_subscription(),
        ts=datetime.fromisoformat(ts.replace("Z", "+00:00")),
        available_at=available,
        observed_at=available + timedelta(seconds=3),
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
            observed_at=base + timedelta(hours=index + 1, seconds=3),
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
            observed_at=hourly.observed_at,
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
        """A new bar and a same-ts correction keep the order they arrived in."""
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


class _CountingDurableSink(_DurableSink):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def __call__(self, symbol, timeframe, bar, ts) -> None:
        self.attempts += 1
        super().__call__(symbol, timeframe, bar, ts)


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

    from tests.engine.live_factories import _test_cfg, make_live_runner

    state = bars if bars is not None else {"n": 6}
    return make_live_runner(
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

    def test_a_restart_keeps_the_queue_it_restored(self) -> None:
        """Without a new bar the restart's refetch delivers nothing new, so only
        the restored queue carries the row the crashed process accepted."""
        from librae.live.state import MemoryLiveStateStore

        store = MemoryLiveStateStore()
        failing = _DurableSink()
        failing.fail = True
        crashed = _runner(failing, store=store)
        crashed._poll_cycle()
        queued = [row.identity for row in crashed._pending_ohlcv]

        restarted = _runner(_DurableSink(), store=store)

        assert queued
        assert [row.identity for row in restarted._pending_ohlcv] == queued

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

        from tests.engine.live_factories import _test_cfg, make_live_runner

        runner = make_live_runner(
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


class TestARefusedReReadDoesNotSkipTheDecision:
    """A restart re-reads bars already decided and offers them to the sink
    while fetching. If the sink is down, that must not abort the cycle and
    leave the new bar undecided until the next poll."""

    @staticmethod
    def _restart_while_the_sink_is_down():
        from librae.core.strategy import Strategy
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        evaluated: list = []

        class Count(Strategy):
            def on_bar(self, ctx):
                evaluated.append(ctx.ts)
                return []

        store = MemoryLiveStateStore()
        bars = {"n": 6}

        def runner(sink):
            return make_live_runner(
                strategy=Count(),
                config=_test_cfg(mode="sim", warmup_periods=1),
                state_store=store,
                fetcher=lambda _s, _tf, limit, **_k: _frame(bars["n"]).tail(limit),
                on_ohlcv=sink,
            )

        runner(_DurableSink())._poll_cycle()
        bars["n"] = 7
        down = _DurableSink()
        down.fail = True
        restarted = runner(down)
        evaluated.clear()
        return restarted, evaluated

    def test_the_new_bar_is_still_decided(self) -> None:
        restarted, evaluated = self._restart_while_the_sink_is_down()

        restarted._poll_cycle()

        assert evaluated == [datetime(2025, 1, 1, 6, tzinfo=UTC)]

    def test_the_decided_bar_waits_in_the_queue(self) -> None:
        restarted, _ = self._restart_while_the_sink_is_down()

        restarted._poll_cycle()

        assert [row.ts for row in restarted._pending_ohlcv] == [datetime(2025, 1, 1, 6, tzinfo=UTC)]


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

        from tests.engine.live_factories import _test_cfg

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
            patch("librae.db.timescale_writer.write_run_bars", boom),
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

        from tests.engine.live_factories import make_live_runner

        config, callbacks = self._callbacks()
        accepted: list = []

        def record(run_id, subscription, df, *_args, **_kwargs):
            accepted.append((run_id, subscription.symbol, len(df)))
            return len(df)

        def boom(*_args, **_kwargs):
            raise RuntimeError("timescale unavailable")

        bars = {"n": 6}
        runner = make_live_runner(
            config=config,
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: _frame(bars["n"]),
            on_ohlcv=callbacks.on_ohlcv,
        )

        with patch("librae.db.timescale_writer.write_run_bars", boom):
            runner._poll_cycle()
        assert runner._pending_ohlcv, "a failed reference write must stay pending"

        bars["n"] = 7
        with patch("librae.db.timescale_writer.write_run_bars", record):
            runner._poll_cycle()

        assert accepted, "the row the failed write dropped must reach the writer"
        assert {run_id for run_id, _symbol, _rows in accepted} == {"r1"}
        assert runner._pending_ohlcv == []


class TestTheCheckpointRecordsWhatTheSinkAccepted:
    """`flush_pending` truncates the queue before it persists.

    Asserted on the queue as `persist` saw it, not on the list afterwards:
    both orderings leave the same list behind, which is why swapping the two
    statements leaves the rest of the suite green. Persisting first records
    rows the sink already took, so a crash before the next checkpoint
    restarts with them still queued — harmless to redeliver, but they hold
    headroom against MAX_PENDING_OHLCV, whose overflow drops rows.
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
        assert checkpointed == expected_tail

    def test_a_sink_that_takes_nothing_writes_no_checkpoint(self) -> None:
        pending = _pending_sequence(2)
        untouched = [row.identity for row in pending]

        def sink(*args: object) -> None:
            raise RuntimeError("sink is down")

        assert self._flush(pending, sink) is None
        assert [row.identity for row in pending] == untouched


class TestQueuedRowsGoInBatches:
    """A sink with a batch path takes a run of one symbol's queued rows in
    one call, in ts order, acknowledged whole or not at all."""

    @staticmethod
    def _flush(pending, batch):
        from librae.live.ohlcv_audit import flush_pending

        singles: list[datetime] = []
        flush_pending(
            pending,
            on_ohlcv=lambda _symbol, _tf, _bar, ts: singles.append(ts),
            on_ohlcv_batch=batch,
            degraded=False,
            persist=lambda: None,
        )
        return singles

    def test_a_batch_breaks_where_the_symbol_or_ts_order_does(self) -> None:
        first, second, third = _pending_sequence(3)
        other = replace(second, subscription=replace(_subscription(), symbol="ETHUSDT"))
        correction = replace(first, available_at=first.available_at + timedelta(minutes=5))
        pending = [first, second, third, correction, other]
        batches: list[tuple[str, list[datetime]]] = []

        singles = self._flush(
            pending, lambda symbol, _tf, bars: batches.append((symbol, [ts for ts, _ in bars]))
        )

        assert batches == [("BTCUSDT", [first.ts, second.ts, third.ts])]
        assert singles == [correction.ts, other.ts]
        assert pending == []

    def test_a_refused_batch_stays_queued_whole(self) -> None:
        pending = _pending_sequence(3)
        queued = list(pending)

        def refuse(*_args: object) -> None:
            raise RuntimeError("timescale unavailable")

        assert self._flush(pending, refuse) == []
        assert pending == queued


class _RecordingDurableSink(_DurableSink):
    """A durable sink that keeps each delivered bar, clocks included."""

    def __init__(self) -> None:
        super().__init__()
        self.bars: list[tuple[datetime, dict]] = []

    def __call__(self, symbol, timeframe, bar, ts) -> None:
        super().__call__(symbol, timeframe, bar, ts)
        self.bars.append((ts, dict(bar)))


class _DurableBatches(list):
    """The batch path of a durable sink, keeping each call's symbol and bar times."""

    durable_ohlcv_delivery = True

    def __call__(self, symbol, _timeframe, bars) -> None:
        self.append((symbol, [ts for ts, _bar in bars]))


class TestTheAuditRowSaysWhenTheBarWasReceived:
    """observed_at is when the engine received a version, not when it was stored.

    A failed write and the retry that lands are different moments. Stamped at
    the retry, the row would say the run saw the bar after it traded on it.
    """

    RECEIVED = datetime(2025, 1, 1, 10, tzinfo=UTC)
    LAST_BAR = datetime(2025, 1, 1, 5, tzinfo=UTC)

    @classmethod
    def _runner(cls, sink, clock, *, store=None, frame=None):
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        return make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=1),
            state_store=store or MemoryLiveStateStore(),
            fetcher=lambda *a, **k: frame["bars"] if frame else _frame(6),
            on_ohlcv=sink,
            clock=lambda: clock["now"],
        )

    def test_the_decided_bar_carries_its_receipt_time(self) -> None:
        sink = _RecordingDurableSink()
        runner = self._runner(sink, {"now": self.RECEIVED})

        runner._poll_cycle()

        ts, bar = sink.bars[-1]
        assert ts == self.LAST_BAR
        assert bar["observed_at"] == self.RECEIVED
        assert bar["available_at"] == self.LAST_BAR + timedelta(hours=1)

    def test_a_retried_row_keeps_the_time_it_was_received(self) -> None:
        sink = _RecordingDurableSink()
        sink.fail = True
        clock = {"now": self.RECEIVED}
        runner = self._runner(sink, clock)
        runner._poll_cycle()

        clock["now"] = self.RECEIVED + timedelta(minutes=30)
        sink.fail = False
        runner._poll_cycle()

        assert [bar["observed_at"] for ts, bar in sink.bars if ts == self.LAST_BAR] == [
            self.RECEIVED
        ]

    def test_a_restart_replays_the_time_the_crashed_process_received_it(self) -> None:
        from librae.live.state import MemoryLiveStateStore

        store = MemoryLiveStateStore()
        failing = _RecordingDurableSink()
        failing.fail = True
        self._runner(failing, {"now": self.RECEIVED}, store=store)._poll_cycle()

        recovered = _RecordingDurableSink()
        restarted_at = self.RECEIVED + timedelta(hours=2)
        self._runner(recovered, {"now": restarted_at}, store=store)._poll_cycle()

        # The queued row comes first, so a writer that keeps the first copy of
        # a version keeps this one; the restart's re-read follows it.
        stamps = [bar["observed_at"] for ts, bar in recovered.bars if ts == self.LAST_BAR]
        assert stamps[0] == self.RECEIVED
        assert set(stamps[1:]) <= {restarted_at}

    def test_a_version_fetched_again_keeps_its_first_receipt(self) -> None:
        clock = {"now": self.RECEIVED}
        runner = self._runner(None, clock)
        runner._fetch_with_cache_unchecked("BTCUSDT")
        clock["now"] = self.RECEIVED + timedelta(minutes=5)
        runner._fetch_with_cache_unchecked("BTCUSDT")

        latest = runner._ohlcv_cache["BTCUSDT"].iloc[-1]
        assert runner._observed_at("BTCUSDT", latest["ts"], latest["available_at"]) == (
            self.RECEIVED
        )

    def test_a_correction_is_a_new_version_received_when_it_arrives(self) -> None:
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = self._runner(sink, clock, frame=frame)
        runner._poll_cycle()

        corrected = _frame(6)
        corrected["available_at"] = corrected["ts"] + pd.Timedelta(hours=1)
        corrected.loc[5, ["close", "available_at"]] = [100.5, self.RECEIVED + timedelta(minutes=15)]
        frame["bars"] = corrected
        clock["now"] = self.RECEIVED + timedelta(minutes=30)
        runner._poll_cycle()

        versions = [
            (bar["available_at"], bar["observed_at"], bar["close"])
            for ts, bar in sink.bars
            if ts == self.LAST_BAR
        ]
        assert versions == [
            (self.LAST_BAR + timedelta(hours=1), self.RECEIVED, 100.0),
            (self.RECEIVED + timedelta(minutes=15), self.RECEIVED + timedelta(minutes=30), 100.5),
        ]

    def test_a_revision_under_the_same_available_at_is_a_new_version(self, caplog) -> None:
        """A source can change a closed bar without moving the available_at
        its calendar derives. Kept silently, the cache and every later window
        would read the first values while the source says otherwise."""
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = self._runner(sink, clock, frame=frame)
        runner._poll_cycle()

        revised = _frame(6)
        revised.loc[5, ["close", "volume"]] = [100.5, 1_200.0]
        frame["bars"] = revised
        clock["now"] = self.RECEIVED + timedelta(minutes=30)
        with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
            runner._poll_cycle()
            clock["now"] = self.RECEIVED + timedelta(minutes=40)
            runner._poll_cycle()

        versions = [
            (bar["available_at"], bar["observed_at"], bar["close"], bar["volume"])
            for ts, bar in sink.bars
            if ts == self.LAST_BAR
        ]
        available_at = self.LAST_BAR + timedelta(hours=1)
        assert versions == [
            (available_at, self.RECEIVED, 100.0, 1_000.0),
            (available_at, self.RECEIVED + timedelta(minutes=30), 100.5, 1_200.0),
        ]
        assert runner._ohlcv_cache["BTCUSDT"].iloc[-1]["close"] == 100.5
        assert caplog.text.count("changed under the same available_at") == 1

    def test_the_next_decision_reads_the_revised_bar(self) -> None:
        from tests.engine.live_factories import _simple_feature_fn, _test_cfg, make_live_runner

        windows: list[pd.DataFrame] = []

        def feature_fn(history: pd.DataFrame) -> pd.DataFrame:
            windows.append(history.copy())
            return _simple_feature_fn(history)

        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=2),
            fetcher=lambda *a, **k: frame["bars"],
            feature_fn=feature_fn,
            clock=lambda: clock["now"],
        )
        runner._poll_cycle()

        revised = _frame(7)
        revised.loc[5, "close"] = 100.5
        frame["bars"] = revised
        clock["now"] = self.RECEIVED + timedelta(hours=1)
        runner._poll_cycle()

        assert windows[-1].index[-1] == pd.Timestamp(self.LAST_BAR + timedelta(hours=1))
        assert windows[-1].loc[pd.Timestamp(self.LAST_BAR), "close"] == 100.5

    def test_a_tie_with_the_same_values_keeps_the_cached_row(self) -> None:
        """A fetch can lack a column the cached row carries, such as a
        financing rate whose endpoint failed this poll."""
        runner = self._runner(None, {"now": self.RECEIVED})
        cached = _frame(2).assign(available_at=lambda f: f["ts"] + pd.Timedelta(hours=1))
        cached["funding_rate"] = [0.0, 0.0001]
        fetched = cached.drop(columns="funding_rate")

        merged = runner._merge_runtime_rows("BTCUSDT", cached, fetched)

        assert merged["funding_rate"].tolist() == [0.0, 0.0001]

    def test_a_revision_keeps_a_column_the_fetch_lacks(self) -> None:
        runner = self._runner(None, {"now": self.RECEIVED})
        cached = _frame(2).assign(available_at=lambda f: f["ts"] + pd.Timedelta(hours=1))
        cached["funding_rate"] = [0.0, 0.0001]
        fetched = cached.drop(columns="funding_rate")
        fetched.loc[1, "close"] = 100.5

        merged = runner._merge_runtime_rows("BTCUSDT", cached, fetched)

        assert merged["close"].tolist() == [100.0, 100.5]
        assert merged["funding_rate"].tolist() == [0.0, 0.0001]

    def test_a_fetch_that_revises_a_bar_twice_records_what_the_cache_kept(
        self, monkeypatch
    ) -> None:
        """A warmup ladder or a history walk merges several responses in one
        fetch, so the same bar can be revised twice before it is recorded."""
        sink = _RecordingDurableSink()
        runner = self._runner(sink, {"now": self.RECEIVED})
        bar = {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "volume": 1_000.0,
            "available_at": pd.Timestamp(self.LAST_BAR + timedelta(hours=1)),
            "observed_at": self.RECEIVED,
        }

        def fetch(symbol, *, audit_sink, correction_sink):
            correction_sink.extend([(self.LAST_BAR, {**bar, "close": c}) for c in (100.5, 100.7)])
            return _frame(6)

        monkeypatch.setattr(runner, "_fetch_with_cache_unchecked", fetch)
        runner._fetch_runtime_frames()

        assert [recorded["close"] for _ts, recorded in sink.bars] == [100.7]

    @staticmethod
    def _warmup_runner(sink, clock, frame):
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        return make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=5),
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: frame["bars"],
            on_ohlcv=sink,
            clock=lambda: clock["now"],
        )

    def test_the_first_decision_records_the_window_it_read(self) -> None:
        """Bars a decision read but did not decide on would otherwise reach
        the table only at a later restart, stamped with its time."""
        sink = _RecordingDurableSink()
        runner = self._warmup_runner(sink, {"now": self.RECEIVED}, {"bars": _frame(6)})

        runner._poll_cycle()

        assert sorted(ts for ts, _bar in sink.bars) == [
            self.LAST_BAR - timedelta(hours=hours) for hours in range(4, -1, -1)
        ]
        assert {bar["observed_at"] for _ts, bar in sink.bars} == {self.RECEIVED}
        assert sink.bars[0][0] == self.LAST_BAR, "the decided bar is recorded first"

    def test_bars_that_arrive_while_warmup_is_short_are_recorded_by_the_decision(self) -> None:
        """The first decision can come polls after the start; the window it
        reads is recorded then, with when each bar was received."""
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(3)}
        runner = self._warmup_runner(sink, clock, frame)
        runner._poll_cycle()
        assert sink.bars == [], "no decision while warmup is short"

        frame["bars"] = _frame(8)
        second_poll = self.RECEIVED + timedelta(hours=1)
        clock["now"] = second_poll
        runner._poll_cycle()

        stamps = {ts: bar["observed_at"] for ts, bar in sink.bars}
        decided = datetime(2025, 1, 1, 7, tzinfo=UTC)
        assert sorted(stamps) == [decided - timedelta(hours=hours) for hours in range(4, -1, -1)]
        assert stamps[decided - timedelta(hours=4)] == second_poll
        assert stamps[decided] == second_poll

    def test_steady_polls_record_only_the_decided_bar(self) -> None:
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = self._warmup_runner(sink, clock, frame)
        runner._poll_cycle()
        recorded = len(sink.bars)

        frame["bars"] = _frame(7)
        clock["now"] = self.RECEIVED + timedelta(hours=1)
        runner._poll_cycle()

        assert [ts for ts, _bar in sink.bars[recorded:]] == [self.LAST_BAR + timedelta(hours=1)]

    def test_a_sink_that_takes_batches_gets_the_window_in_one_call(self) -> None:
        """One write per bar would hold a many-symbol start up for minutes."""
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        sink = _RecordingDurableSink()
        batches = _DurableBatches()
        runner = make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=5),
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: _frame(6),
            on_ohlcv=sink,
            on_ohlcv_batch=batches,
            clock=lambda: self.RECEIVED,
        )

        runner._poll_cycle()

        assert [ts for ts, _bar in sink.bars] == [self.LAST_BAR]
        assert batches == [
            ("BTCUSDT", [self.LAST_BAR - timedelta(hours=hours) for hours in range(4, 0, -1)])
        ]

    def test_a_batch_path_without_its_sink_is_refused(self) -> None:
        """Window bars go to on_ohlcv_batch only alongside on_ohlcv; alone it
        would be silently ignored."""
        from tests.engine.live_factories import _test_cfg, make_live_runner

        with pytest.raises(ValueError, match="pass both"):
            make_live_runner(
                config=_test_cfg(mode="sim"),
                on_ohlcv=None,
                on_ohlcv_batch=lambda *args: None,
            )

    def test_the_window_waits_out_a_sink_that_refused_the_decided_bar(self) -> None:
        """Offering it anyway would only wait on the same failing database."""
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        sink = _RecordingDurableSink()
        sink.fail = True
        batches = _DurableBatches()
        runner = make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=5),
            state_store=MemoryLiveStateStore(),
            fetcher=lambda *a, **k: _frame(6),
            on_ohlcv=sink,
            on_ohlcv_batch=batches,
            clock=lambda: self.RECEIVED,
        )

        runner._poll_cycle()

        assert runner._pending_ohlcv, "the decided bar stays queued"
        assert batches == []

    def test_a_batch_holds_one_version_per_bar(self) -> None:
        """The writer refuses two versions of one bar in one batch."""
        batches: list[list[datetime]] = []
        runner = self._runner(_RecordingDurableSink(), {"now": self.RECEIVED})
        runner._on_ohlcv_batch = lambda _symbol, _tf, bars: batches.append(
            [ts for ts, _bar in bars]
        )
        first, second = self.LAST_BAR - timedelta(hours=1), self.LAST_BAR
        rows = [
            ("BTCUSDT", first, {}),
            ("BTCUSDT", second, {}),
            ("BTCUSDT", second, {}),
        ]

        runner._offer_audit_rows(rows, "test")

        assert batches == [[first, second], [second]]

    def test_a_correction_the_sink_refuses_waits_in_the_queue(self) -> None:
        """A later version of a decided bar is what the audit keeps, so an
        outage must not drop it the way it drops a best-effort re-read."""
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = self._runner(sink, clock, frame=frame)
        runner._poll_cycle()

        corrected = _frame(6)
        corrected["available_at"] = corrected["ts"] + pd.Timedelta(hours=1)
        corrected_at = self.RECEIVED + timedelta(minutes=15)
        corrected.loc[5, ["close", "available_at"]] = [100.5, corrected_at]
        frame["bars"] = corrected
        received = self.RECEIVED + timedelta(minutes=30)
        clock["now"] = received
        sink.fail = True
        runner._poll_cycle()

        assert [(row.available_at, row.observed_at) for row in runner._pending_ohlcv] == [
            (corrected_at, received)
        ]

        sink.fail = False
        clock["now"] = self.RECEIVED + timedelta(hours=1)
        runner._poll_cycle()

        assert runner._pending_ohlcv == []
        ts, bar = sink.bars[-1]
        assert (ts, bar["available_at"], bar["observed_at"]) == (
            self.LAST_BAR,
            corrected_at,
            received,
        )

    def test_a_halted_simulation_still_records_the_bars_it_marks(self) -> None:
        """Equity keeps being marked while halted; the price behind it must
        be in the table too, or a dashboard marks positions at a stale bar."""
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = self._runner(sink, clock, frame=frame)
        runner._poll_cycle()
        runner._halted = True

        frame["bars"] = _frame(7)
        clock["now"] = self.RECEIVED + timedelta(hours=1)
        runner._poll_cycle()

        assert sink.bars[-1][0] == self.LAST_BAR + timedelta(hours=1)
        assert runner._pending_ohlcv == []

    def test_the_stamps_are_pruned_with_the_cache(self) -> None:
        """Kept only for the cached window, so a long run cannot grow them."""
        clock = {"now": self.RECEIVED}
        runner = self._runner(None, clock)
        runner._fetch_with_cache_unchecked("BTCUSDT")

        head = pd.Timestamp(runner._ohlcv_cache["BTCUSDT"]["ts"].iloc[0]).value
        assert runner._bar_observed_at["BTCUSDT"]
        assert all(ts >= head for ts, _available in runner._bar_observed_at["BTCUSDT"])


class TestARestartReReadsOnlyWhatADecisionRead:
    """A row in run_bars reads as a bar a decision of the run read. A restart
    can fetch further back than any window; those bars stay out."""

    def test_bars_older_than_the_last_window_are_not_recorded(self) -> None:
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        store = MemoryLiveStateStore()
        frame = {"bars": _frame(6)}

        def runner(sink):
            # Returns more than it is asked for, as a database-backed warmup
            # or a venue's whole page can.
            return make_live_runner(
                config=_test_cfg(mode="sim", warmup_periods=2),
                state_store=store,
                fetcher=lambda *a, **k: frame["bars"],
                on_ohlcv=sink,
            )

        runner(_RecordingDurableSink())._poll_cycle()
        decided = datetime(2025, 1, 1, 5, tzinfo=UTC)

        frame["bars"] = _frame(7)
        recovered = _RecordingDurableSink()
        runner(recovered)._poll_cycle()

        assert sorted({ts for ts, _bar in recovered.bars}) == [
            decided - timedelta(hours=1),
            decided,
            decided + timedelta(hours=1),
        ]

    def test_a_crash_right_after_the_first_decision_still_records_its_window(self) -> None:
        """The first decision's checkpoint lands before its watermark; a crash
        before its window is offered must not lose the window once the
        market moves on."""
        from librae.live.state import MemoryLiveStateStore

        from tests.engine.live_factories import _test_cfg, make_live_runner

        class Crash(BaseException):
            pass

        store = MemoryLiveStateStore()
        frame = {"bars": _frame(6)}

        def runner(sink):
            return make_live_runner(
                config=_test_cfg(mode="sim", warmup_periods=5),
                state_store=store,
                fetcher=lambda *a, **k: frame["bars"],
                on_ohlcv=sink,
            )

        first = runner(_RecordingDurableSink())

        def crash(*_args: object) -> None:
            raise Crash

        first._deliver_ohlcv = crash
        with pytest.raises(Crash):
            first._poll_cycle()
        decided = datetime(2025, 1, 1, 5, tzinfo=UTC)

        frame["bars"] = _frame(8)
        recovered = _RecordingDurableSink()
        runner(recovered)._poll_cycle()

        recorded = {ts for ts, _bar in recovered.bars}
        assert {decided - timedelta(hours=hours) for hours in range(5)} <= recorded


class TestABurstOfCorrectionsIsNotDropped:
    BOUND = 2
    RECEIVED = datetime(2025, 1, 1, 10, tzinfo=UTC)
    RESTAMPED_AT = RECEIVED + timedelta(minutes=15)

    def _restamp_every_cached_bar(self, monkeypatch, *, before_second_poll=None):
        from tests.engine.live_factories import _test_cfg, make_live_runner

        monkeypatch.setattr("librae.live.engine.MAX_PENDING_OHLCV", self.BOUND)
        monkeypatch.setattr("librae.live.ohlcv_audit.MAX_PENDING_OHLCV", self.BOUND)
        sink = _RecordingDurableSink()
        clock = {"now": self.RECEIVED}
        frame = {"bars": _frame(6)}
        runner = make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=5),
            fetcher=lambda *a, **k: frame["bars"],
            on_ohlcv=sink,
            clock=lambda: clock["now"],
        )
        events: list = []
        runner._on_runtime_event = events.append
        runner._poll_cycle()

        frame["bars"] = _frame(6).assign(close=100.5, available_at=self.RESTAMPED_AT)
        clock["now"] = self.RECEIVED + timedelta(minutes=30)
        if before_second_poll is not None:
            before_second_poll(runner)
        runner._poll_cycle()
        restamped = [ts for ts, bar in sink.bars if bar["available_at"] == self.RESTAMPED_AT]
        dropped = [e for e in events if e.detail.get("reason") == "ohlcv_audit_delivery_failed"]
        return runner, restamped, dropped

    def test_a_healthy_sink_takes_more_corrections_than_the_queue_holds(self, monkeypatch) -> None:
        runner, restamped, dropped = self._restamp_every_cached_bar(monkeypatch)

        assert len(restamped) == 5 > self.BOUND
        assert runner._pending_ohlcv == []
        assert dropped == []

    def test_a_failed_checkpoint_mid_burst_still_queues_the_rest(self, monkeypatch) -> None:
        """The cache already holds the new versions, so a correction left
        unqueued is never offered again."""

        def fail_one_checkpoint(runner):
            persist = runner._persist_state
            calls = {"n": 0}

            def flaky(*orders):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("checkpoint store blinked")
                persist(*orders)

            runner._persist_state = flaky

        runner, restamped, dropped = self._restamp_every_cached_bar(
            monkeypatch, before_second_poll=fail_one_checkpoint
        )

        assert len(restamped) == 5
        assert runner._pending_ohlcv == []
        assert dropped == []

    def test_a_sink_that_takes_batches_gets_the_burst_in_batches(self, monkeypatch) -> None:
        """The burst is flushed during the fetch, before the decision: one
        write per correction would hold the decision for each."""
        batches: list[int] = []

        def take_batches(runner):
            sink = runner._on_ohlcv

            def batch(_symbol, _timeframe, bars):
                batches.append(len(bars))
                sink.bars.extend((ts, dict(bar)) for ts, bar in bars)

            runner._on_ohlcv_batch = batch

        runner, restamped, dropped = self._restamp_every_cached_bar(
            monkeypatch, before_second_poll=take_batches
        )

        assert len(restamped) == 5
        assert runner._pending_ohlcv == []
        assert dropped == []
        # Each flush the full queue triggers is one call; the last row alone
        # goes through on_ohlcv.
        assert batches == [self.BOUND, self.BOUND]

    def test_a_refusing_sink_is_not_asked_again_for_every_correction(self, monkeypatch) -> None:
        """With the queue full and the database down, one write attempt per
        correction would only repeat the failure."""
        down = _CountingDurableSink()
        down.fail = True

        def refuse_with_a_full_queue(runner):
            runner._pending_ohlcv[:] = _pending_sequence(self.BOUND)
            runner._on_ohlcv = down

        _runner, _restamped, dropped = self._restamp_every_cached_bar(
            monkeypatch, before_second_poll=refuse_with_a_full_queue
        )

        assert len(dropped) == 5
        # The cycle's own retry, the one flush the burst triggers, and the
        # flush after the fetch; not one more per correction.
        assert down.attempts == 3
