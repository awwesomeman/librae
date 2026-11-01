"""A live run asks its order adapter to verify the venue before anything else runs."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests.engine.live_factories import _test_cfg, make_live_runner

ORDER_ADAPTER_METHODS = [
    "prepare_order",
    "place_order",
    "find_order",
    "get_order",
    "list_open_orders",
    "cancel_order",
    "get_position",
    "execution_identity",
]


def test_live_start_fails_closed_when_the_adapter_refuses_the_venue():
    adapter = MagicMock()
    adapter.verify_venue_setup.side_effect = ValueError("account is in hedge position mode")

    with pytest.raises(ValueError, match="hedge position mode"):
        make_live_runner(config=_test_cfg(mode="live"), order_adapter=adapter)

    (instruments,) = adapter.verify_venue_setup.call_args.args
    assert instruments["BTCUSDT"].instrument_type == "spot"
    adapter.get_position.assert_not_called()


def test_live_start_runs_without_the_capability():
    adapter = MagicMock(spec_set=ORDER_ADAPTER_METHODS)

    make_live_runner(config=_test_cfg(mode="live"), order_adapter=adapter)


def test_sim_run_never_asks_the_order_adapter():
    adapter = MagicMock()

    make_live_runner(order_adapter=adapter)

    adapter.verify_venue_setup.assert_not_called()
