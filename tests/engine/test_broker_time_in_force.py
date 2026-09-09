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


def _validate(intent: OrderIntent, *, broker: str | None, routes: dict | None = None) -> None:
    """Preflight one intent under a run whose venue is *broker*.

    *routes* mirrors instrument_overrides[symbol]["broker"], which wins over
    the run-level setting.
    """
    resolved = dict(routes or {})

    validate_strategy_decision(
        [intent],
        {"X"},
        primary_symbol="X",
        bars={"X": {"close": 100.0}},
        positions={},
        broker_for=lambda symbol: resolved.get(symbol, broker),
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


class TestTheVenueIsResolvedPerSymbol:
    """instrument_overrides[symbol]["broker"] wins over the run-level broker.

    Checking the run's broker against a symbol routed elsewhere is wrong in
    both directions: it misses a real violation, and — worse — it rejects a
    combination the symbol's actual venue accepts, killing a valid run at
    decision time.
    """

    def test_the_overriding_venue_is_the_one_enforced(self) -> None:
        with pytest.raises(ValueError, match="shioaji does not support"):
            _validate(_resting("gtc"), broker="ibkr", routes={"X": "shioaji"})

    def test_a_symbol_routed_to_a_permissive_venue_is_not_rejected(self) -> None:
        _validate(_resting("gtc"), broker="shioaji", routes={"X": "ibkr"})

    def test_an_override_applies_with_no_run_level_broker(self) -> None:
        with pytest.raises(ValueError, match="shioaji does not support"):
            _validate(_resting("gtc"), broker=None, routes={"X": "shioaji"})

    def test_only_the_intents_own_symbol_decides(self) -> None:
        _validate(_resting("gtc"), broker="ibkr", routes={"OTHER": "shioaji"})

    def test_a_non_primary_symbol_is_resolved_on_its_own_route(self) -> None:
        """The lookup key must be the intent's symbol, not the run's primary.

        With every intent on the primary symbol the two are the same
        expression, so this is the case that tells them apart.
        """
        routed = OrderIntent(action="long", symbol="Y", limit_price=99.0, time_in_force="gtc")

        with pytest.raises(ValueError, match="shioaji does not support"):
            validate_strategy_decision(
                [routed],
                {"X", "Y"},
                primary_symbol="X",
                bars={"X": {"close": 100.0}, "Y": {"close": 100.0}},
                positions={},
                broker_for=lambda symbol: {"Y": "shioaji"}.get(symbol, "ibkr"),
            )

    def test_the_primary_symbols_route_does_not_leak_onto_another_symbol(self) -> None:
        permitted = OrderIntent(action="long", symbol="Y", limit_price=99.0, time_in_force="gtc")

        validate_strategy_decision(
            [permitted],
            {"X", "Y"},
            primary_symbol="X",
            bars={"X": {"close": 100.0}, "Y": {"close": 100.0}},
            positions={},
            broker_for=lambda symbol: {"X": "shioaji"}.get(symbol, "ibkr"),
        )


class TestTheResolverIsTheOneRule:
    def test_run_config_resolves_the_override_over_the_run_broker(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        config = RunConfig(
            strategy_name="s",
            symbols=["X", "Y"],
            timeframe="1d",
            market="us_equity",
            data_source="local",
            account=AccountConfig(currency="USD", initial_cash=1_000.0),
            mode="backtest",
            broker="ibkr",
            instrument_overrides={"X": {"broker": "shioaji"}},
        )

        assert config.broker_for("X") == "shioaji"
        assert config.broker_for("Y") == "ibkr"

    def test_no_broker_anywhere_resolves_to_none(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        config = RunConfig(
            strategy_name="s",
            symbols=["X"],
            timeframe="1d",
            market="us_equity",
            data_source="local",
            account=AccountConfig(currency="USD", initial_cash=1_000.0),
            mode="backtest",
        )

        assert config.broker_for("X") is None
