"""Which broker name owns which venue capability table.

Only routing lives here. Each table is declared next to the adapter that
owns the venue knowledge and is enforced by that adapter's ``place_order``,
so a capability this module reports is the one the venue call will apply.

A caller-supplied adapter factory registers a name this module has never
heard of; an unknown name is therefore not an error, it is simply not
checked here and stays the adapter's own business at submission.
"""

from __future__ import annotations

from collections.abc import Mapping

from .base import validate_time_in_force
from .crypto_adapter import SUPPORTED_TIME_IN_FORCE as _CRYPTO_TIME_IN_FORCE
from .ibkr_adapter import SUPPORTED_TIME_IN_FORCE as _IBKR_TIME_IN_FORCE
from .shioaji_adapter import SUPPORTED_TIME_IN_FORCE as _SHIOAJI_TIME_IN_FORCE

# Keys are the broker names librae.orchestration.live resolves to an adapter.
# "crypto" and "binance" resolve to a Binance-family exchange there, which is
# what the table describes. A caller who registers an adapter factory for a
# different exchange under one of these names is naming it something it is
# not; their adapter stays the authority, as for any name not listed here.
BROKER_TIME_IN_FORCE: Mapping[str, Mapping[str, frozenset[str]]] = {
    "binance": _CRYPTO_TIME_IN_FORCE,
    "crypto": _CRYPTO_TIME_IN_FORCE,
    "ibkr": _IBKR_TIME_IN_FORCE,
    "shioaji": _SHIOAJI_TIME_IN_FORCE,
}


def validate_broker_time_in_force(broker: str, order_type: str, time_in_force: str) -> None:
    """Reject a lifetime the configured broker cannot express, before an order exists."""
    supported = BROKER_TIME_IN_FORCE.get(broker)
    if supported is None:
        return
    validate_time_in_force(broker, supported, order_type, time_in_force)
