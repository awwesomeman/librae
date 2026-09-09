"""Whether a halted run may start a new risk epoch, and why not.

Readiness is a pure query over the book and its marks, so an operator or a
health probe can read the blocking reason without provoking the exception
that used to be the only way to obtain it. ``LiveTrader`` binds its own
state; nothing here reads or mutates the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Container, Iterable, Mapping
from datetime import datetime, timedelta
from typing import NoReturn

from librae.core.executor import RuntimeEvent
from librae.core.market_data import MarketDataSubscription
from librae.core.readiness import evaluate_observation

from .interfaces import RuntimeEventCallback
from .state import HaltResetReadiness


def evaluate_reset_readiness(
    *,
    active_orders: Collection[object],
    positions: Iterable[str],
    last_prices: Container[str],
    last_bar_ts: Mapping[str, datetime],
    subscriptions: Mapping[str, MarketDataSubscription],
    now: datetime,
    staleness_grace: Callable[[MarketDataSubscription], timedelta],
) -> HaltResetReadiness:
    """Report whether a halt reset is allowed, and the blocking reason."""
    if active_orders:
        return HaltResetReadiness(
            ready=False,
            reason="unresolved_broker_orders",
            required_action=("resolve or cancel every tracked broker order, then reset again"),
        )
    missing: list[str] = []
    stale: list[str] = []
    for symbol in sorted(positions):
        if symbol not in last_prices:
            missing.append(symbol)
            continue
        subscription = subscriptions.get(symbol)
        observed_at = last_bar_ts.get(symbol)
        if subscription is None or observed_at is None:
            missing.append(symbol)
            continue
        status = evaluate_observation(
            observed_at,
            as_of=now,
            timeframe=subscription.timeframe,
            calendar_id=subscription.calendar_id,
            grace=staleness_grace(subscription),
        )
        if not status.fresh:
            stale.append(symbol)
    if missing:
        return HaltResetReadiness(
            ready=False,
            reason="missing_valuation_mark",
            blocking_symbols=tuple(missing),
            required_action=(
                "wait for a completed bar for each listed symbol; the engine "
                "establishes the mark itself and no operator input is needed"
            ),
        )
    if stale:
        return HaltResetReadiness(
            ready=False,
            reason="stale_valuation_mark",
            blocking_symbols=tuple(stale),
            required_action=(
                "restore the market-data feed for each listed symbol; resetting "
                "on a stale mark would revalue the book at a price the venue has "
                "moved away from"
            ),
        )
    return HaltResetReadiness(ready=True)


def refuse_reset(
    readiness: HaltResetReadiness,
    *,
    on_runtime_event: RuntimeEventCallback | None,
    utc_now: Callable[[], datetime],
) -> NoReturn:
    """Record a blocked reset and fail closed with its structured reason.

    Raising the reason an operator can act on, rather than an unhandled
    valuation error, and recording it so the refusal is visible after the
    fact.
    """
    if on_runtime_event:
        on_runtime_event(
            RuntimeEvent(
                ts=utc_now(),
                event_type="decision_skipped",
                detail={
                    "reason": "halt_reset_blocked",
                    "cause": readiness.reason,
                    "blocking_symbols": list(readiness.blocking_symbols),
                    "required_action": readiness.required_action,
                },
            )
        )
    detail = f" for {list(readiness.blocking_symbols)}" if readiness.blocking_symbols else ""
    raise RuntimeError(
        f"cannot reset halt: {readiness.reason}{detail}. {readiness.required_action}"
    )
