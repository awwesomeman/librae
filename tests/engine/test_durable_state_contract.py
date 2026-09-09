"""Order-capable live operation requires restart-durable state.

The state-store protocol exposes persistence operations but says nothing about
whether they survive the process. A live runner therefore could not tell a
production store from a dictionary, and an integration could reach
order-capable startup with no recovery state at all — the failure only shows
up after a crash, when the book is gone and the broker still holds positions.
"""

from __future__ import annotations

import pytest
from librae.live.state import MemoryLiveStateStore


class _UndeclaredStore:
    """A custom store that implements the protocol and declares nothing."""

    def load(self, state_key):
        return None

    def save(self, state, orders=()):
        return None

    def acquire_lease(self, state_key):
        return True

    def release_lease(self, state_key):
        return None


class TestDurabilityDeclaration:
    def test_the_memory_store_is_not_durable(self) -> None:
        assert MemoryLiveStateStore().restart_durable is False

    def test_it_can_be_declared_durable_for_tests(self) -> None:
        """The escape hatch is explicit and greppable, so a production store
        cannot acquire durability by accident."""
        assert MemoryLiveStateStore(restart_durable_for_tests=True).restart_durable is True

    def test_the_reference_database_store_is_durable(self) -> None:
        from librae.db.timescale_state import TimescaleLiveStateStore

        assert TimescaleLiveStateStore.restart_durable is True


class TestLiveRejectsNonDurableState:
    @staticmethod
    def _live(**kwargs):
        from tests.engine.test_live_runner import (
            TestLiveTrader,
            _mock_order_adapter,
            _test_cfg,
        )

        return TestLiveTrader()._make_runner(
            config=_test_cfg(mode="live"),
            order_adapter=_mock_order_adapter(),
            **kwargs,
        )

    def test_a_memory_store_cannot_run_live(self) -> None:
        with pytest.raises(ValueError, match="restart-durable"):
            self._live(state_store=MemoryLiveStateStore())

    def test_a_store_that_declares_nothing_cannot_run_live(self) -> None:
        """Fail closed: silence is not a durability claim."""
        with pytest.raises(ValueError, match="restart-durable"):
            self._live(state_store=_UndeclaredStore())

    @pytest.mark.parametrize("declared", ["yes", 1, [1], object()])
    def test_a_truthy_non_bool_is_not_a_durability_claim(self, declared: object) -> None:
        """The Protocol types this as bool. A store that declares something
        merely truthy has not made the claim, and accepting it would let a
        typo or a stray attribute pass for durable storage."""
        store = MemoryLiveStateStore()
        store.restart_durable = declared

        with pytest.raises(ValueError, match="restart-durable"):
            self._live(state_store=store)

    def test_a_declared_durable_store_is_accepted(self) -> None:
        trader = self._live(state_store=MemoryLiveStateStore(restart_durable_for_tests=True))

        assert trader is not None

    def test_simulation_still_accepts_a_memory_store(self) -> None:
        """Shadow simulation has no broker order to recover, so process-local
        state stays the right default there."""
        from tests.engine.test_live_runner import TestLiveTrader, _test_cfg

        trader = TestLiveTrader()._make_runner(
            config=_test_cfg(mode="sim"),
            state_store=MemoryLiveStateStore(),
        )

        assert trader is not None

    def test_the_rejection_names_the_store(self) -> None:
        with pytest.raises(ValueError, match="MemoryLiveStateStore"):
            self._live(state_store=MemoryLiveStateStore())


def test_the_protocol_declares_the_capability() -> None:
    """A capability the protocol does not mention cannot be relied on."""
    from librae.live.state import LiveStateStore

    assert "restart_durable" in LiveStateStore.__annotations__


def test_live_trader_still_requires_a_store_at_all() -> None:
    """The durability check must not displace the existing presence check."""
    from tests.engine.test_live_runner import TestLiveTrader, _mock_order_adapter, _test_cfg

    with pytest.raises(ValueError, match="state_store"):
        TestLiveTrader()._make_runner(
            config=_test_cfg(mode="live"),
            order_adapter=_mock_order_adapter(),
            state_store=None,
        )
