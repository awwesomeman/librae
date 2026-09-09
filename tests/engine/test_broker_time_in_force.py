"""A configured broker's order lifetimes are checked before the run, not at submission.

The engine's own preflight stays broker-neutral — it rejects only what a
bar-based simulation cannot express. Venue rules are additional, and they used
to apply for the first time when the adapter built the order: a strategy on
`broker: shioaji` using `time_in_force="gtc"` backtested clean and failed live.
"""

from __future__ import annotations

import pytest
from librae.brokers.capabilities import BROKER_TIME_IN_FORCE, validate_broker_time_in_force
from librae.core.executor import validate_strategy_decision
from librae.core.strategy import OrderIntent


def _validate(intent: OrderIntent, *, broker: str | None) -> None:
    validate_strategy_decision(
        [intent],
        {"X"},
        primary_symbol="X",
        bars={"X": {"close": 100.0}},
        positions={},
        broker=broker,
    )


def _resting(time_in_force: str) -> OrderIntent:
    return OrderIntent(action="long", symbol="X", limit_price=99.0, time_in_force=time_in_force)


class TestAConfiguredBrokerAddsItsVenueRules:
    def test_shioaji_refuses_gtc_before_any_order_is_built(self) -> None:
        with pytest.raises(ValueError, match="shioaji does not support"):
            _validate(_resting("gtc"), broker="shioaji")

    def test_the_message_names_the_broker_and_what_it_accepts(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            _validate(_resting("gtc"), broker="shioaji")

        message = str(excinfo.value)
        assert "shioaji" in message
        assert "'day', 'fok', 'ioc'" in message

    def test_binance_refuses_a_market_lifetime_it_cannot_send(self) -> None:
        market = OrderIntent(action="long", symbol="X", quantity=1.0, time_in_force="fok")

        with pytest.raises(ValueError, match="binance does not support"):
            _validate(market, broker="binance")

    def test_a_supported_combination_passes(self) -> None:
        _validate(_resting("day"), broker="shioaji")
        _validate(_resting("gtc"), broker="ibkr")


class TestNeutralityIsPreserved:
    """DoD: a run without a configured broker gains no venue-specific rejection."""

    def test_no_broker_accepts_what_one_venue_would_refuse(self) -> None:
        _validate(_resting("gtc"), broker=None)

    def test_an_unknown_broker_name_is_left_to_its_adapter(self) -> None:
        # A caller-supplied adapter factory registers a name librae cannot
        # know; guessing at its capabilities would invent a rejection.
        _validate(_resting("gtc"), broker="my-own-venue")

    def test_an_unset_lifetime_is_never_rejected(self) -> None:
        # None resolves per order type at the broker, and every default the
        # engine picks is accepted everywhere.
        for broker in BROKER_TIME_IN_FORCE:
            _validate(OrderIntent(action="long", symbol="X", limit_price=99.0), broker=broker)
            _validate(OrderIntent(action="long", symbol="X", quantity=1.0), broker=broker)


class TestTheTablesDescribeRealVenues:
    def test_every_declared_lifetime_is_one_the_engine_can_express(self) -> None:
        engine_lifetimes = {"day", "gtc", "ioc", "fok"}

        for supported in BROKER_TIME_IN_FORCE.values():
            assert set(supported) == {"limit", "market"}
            for accepted in supported.values():
                assert accepted <= engine_lifetimes
                assert accepted

    def test_the_engine_default_is_supported_by_every_declared_broker(self) -> None:
        # LiveExecutor.OrderRequest resolves None to these; a table that
        # refused one would break a strategy that set no lifetime at all.
        for broker in BROKER_TIME_IN_FORCE:
            validate_broker_time_in_force(broker, "market", "ioc")
            validate_broker_time_in_force(broker, "limit", "day")
