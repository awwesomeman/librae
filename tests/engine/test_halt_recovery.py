"""Halt recovery must be actionable, and must never revalue on bad marks.

Remaining halted is always safe. The gap this closes is that an operator in
the startup window — a restored halted checkpoint, before the first bar — got
an unhandled ``ValueError`` from inside valuation instead of a reason and a
next action. A stale mark was worse: reset succeeded on a price a dead feed
last produced days ago.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from librae.core.strategy import PositionState
from librae.live.state import LiveRuntimeState, MemoryLiveStateStore

CLOCK = datetime(2025, 1, 1, 6, tzinfo=UTC)


def _position(symbol: str = "BTCUSDT") -> PositionState:
    return PositionState(
        symbol=symbol,
        side="long",
        entry_price=100.0,
        quantity=1.0,
        entry_at=datetime(2025, 1, 1, tzinfo=UTC),
        periods_held=1,
        entry_commission=0.0,
        entry_slippage=0.0,
        entry_tax=0.0,
        total_entry_cost=100.0,
    )


def _bars(n: int = 6):
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


def _halted_runner(*, positions=None, marks=None, last_bar_ts=None, clock=CLOCK):
    """A trader restored from a halted checkpoint, before any poll cycle."""
    from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

    config = _test_cfg(mode="sim", warmup_periods=1)
    store = MemoryLiveStateStore()
    store.save(
        LiveRuntimeState(
            state_key=f"sim:{config.config_hash}",
            run_id="r1",
            config_hash=config.config_hash,
            mode="sim",
            account_id="default",
            cash=1_000.0,
            positions=positions if positions is not None else {"BTCUSDT": _position()},
            last_prices=marks or {},
            last_bar_ts=last_bar_ts or {},
            halted=True,
        )
    )
    runner = TestLiveTrader()._make_runner(
        config=config,
        state_store=store,
        fetcher=lambda *a, **k: _bars(),
        clock=lambda: clock,
    )
    runner.STALE_DATA_TOLERANCE_BARS = 2
    return runner


class TestReadinessIsObservable:
    """Health tooling and an operator need the same answer, without having to
    provoke an exception to get it."""

    def test_a_missing_mark_is_reported_not_raised(self) -> None:
        readiness = _halted_runner().halt_reset_readiness()

        assert readiness.ready is False
        assert readiness.reason == "missing_valuation_mark"
        assert readiness.blocking_symbols == ("BTCUSDT",)
        assert readiness.required_action

    def test_a_stale_mark_blocks_reset(self) -> None:
        """A dead feed's last price is not a valuation. Reset used to succeed
        on it, because only the mark's presence was checked."""
        readiness = _halted_runner(
            marks={"BTCUSDT": 100.0},
            last_bar_ts={"BTCUSDT": datetime(2024, 12, 20, tzinfo=UTC)},
        ).halt_reset_readiness()

        assert readiness.ready is False
        assert readiness.reason == "stale_valuation_mark"
        assert readiness.blocking_symbols == ("BTCUSDT",)

    def test_a_fresh_mark_is_ready(self) -> None:
        readiness = _halted_runner(
            marks={"BTCUSDT": 100.0},
            last_bar_ts={"BTCUSDT": CLOCK - timedelta(hours=1)},
        ).halt_reset_readiness()

        assert readiness.ready is True
        assert readiness.reason is None

    def test_a_flat_account_needs_no_marks(self) -> None:
        """Nothing to revalue, so the startup window does not block recovery."""
        readiness = _halted_runner(positions={}).halt_reset_readiness()

        assert readiness.ready is True

    def test_unresolved_orders_are_reported_through_the_same_query(self) -> None:
        runner = _halted_runner(
            marks={"BTCUSDT": 100.0},
            last_bar_ts={"BTCUSDT": CLOCK - timedelta(hours=1)},
        )
        runner._active_orders = [object()]

        readiness = runner.halt_reset_readiness()

        assert readiness.ready is False
        assert readiness.reason == "unresolved_broker_orders"

    def test_the_query_never_mutates_the_halt(self) -> None:
        runner = _halted_runner()

        runner.halt_reset_readiness()

        assert runner._halted is True


class TestResetIsBlockedActionably:
    def test_a_missing_mark_no_longer_raises_from_inside_valuation(self) -> None:
        runner = _halted_runner()

        with pytest.raises(RuntimeError, match="missing_valuation_mark"):
            runner.reset_halt()

    def test_the_error_names_the_required_action(self) -> None:
        runner = _halted_runner()

        with pytest.raises(RuntimeError, match="wait for a completed bar"):
            runner.reset_halt()

    def test_a_blocked_reset_leaves_the_halt_in_place(self) -> None:
        runner = _halted_runner()

        with pytest.raises(RuntimeError):
            runner.reset_halt()

        assert runner._halted is True

    def test_a_blocked_reset_is_auditable(self) -> None:
        runner = _halted_runner()
        events: list = []
        runner._on_runtime_event = events.append

        with pytest.raises(RuntimeError):
            runner.reset_halt()

        reasons = [e.detail.get("reason") for e in events]
        assert "halt_reset_blocked" in reasons
        blocked = next(e for e in events if e.detail.get("reason") == "halt_reset_blocked")
        assert blocked.detail["cause"] == "missing_valuation_mark"
        assert blocked.detail["blocking_symbols"] == ["BTCUSDT"]

    def test_unresolved_orders_still_block(self) -> None:
        """The existing guard rail, preserved and given the same shape."""
        runner = _halted_runner(
            marks={"BTCUSDT": 100.0},
            last_bar_ts={"BTCUSDT": CLOCK - timedelta(hours=1)},
        )
        runner._active_orders = [object()]

        with pytest.raises(RuntimeError, match="unresolved_broker_orders"):
            runner.reset_halt()

    def test_a_stale_mark_blocks_reset(self) -> None:
        runner = _halted_runner(
            marks={"BTCUSDT": 100.0},
            last_bar_ts={"BTCUSDT": datetime(2024, 12, 20, tzinfo=UTC)},
        )

        with pytest.raises(RuntimeError, match="stale_valuation_mark"):
            runner.reset_halt()


class TestResetSucceedsOnceMarksArrive:
    def test_the_first_completed_bar_unblocks_a_non_flat_account(self) -> None:
        """The restart window closes by itself: no operator input required,
        only a usable observation."""
        runner = _halted_runner()
        assert runner.halt_reset_readiness().ready is False

        runner._poll_cycle()

        assert runner.halt_reset_readiness().ready is True
        runner.reset_halt()
        assert runner._halted is False

    def test_a_flat_account_resets_before_any_bar(self) -> None:
        runner = _halted_runner(positions={})

        runner.reset_halt()

        assert runner._halted is False

    def test_reset_starts_a_new_risk_epoch(self) -> None:
        runner = _halted_runner()
        runner._poll_cycle()

        runner.reset_halt()

        assert runner._equity_peak == runner._prev_equity
