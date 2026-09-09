"""Whether the live run has data it can act on, and whether it is current.

Two edge-triggered reports: one for a feed that stopped updating without
raising, one for an input that is absent altogether. Both alert on the way
into the bad state and once on the way out, so a persistent outage does not
page per poll cycle. ``LiveTrader`` keeps thin methods that bind its own
alert-latch state; nothing here reads the engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Container, MutableMapping, MutableSet, Sequence
from datetime import datetime, timedelta

from librae.core.market_data import MarketDataSubscription
from librae.core.readiness import evaluate_observation

logger = logging.getLogger(__name__)


def staleness_grace(
    subscription: MarketDataSubscription,
    *,
    tolerance_bars: int,
) -> timedelta:
    """Bounded publication slack allowed after an expected close.

    Wall-clock on purpose: it models a feed publishing a completed bar
    late, not extra market time. Sized from the subscription's own
    interval so a D1 input is not held to an H1 deadline.
    """
    from librae.core.utils import interval_to_timedelta

    return tolerance_bars * interval_to_timedelta(subscription.timeframe)


def check_staleness(
    symbol: str,
    latest_ts: datetime,
    *,
    subscription: MarketDataSubscription,
    as_of: datetime,
    grace: timedelta,
    stale_alerted: MutableMapping[MarketDataSubscription, bool],
    unanchored_reported: MutableSet[MarketDataSubscription],
    notify: Callable[..., None],
    strategy_name: str,
) -> bool:
    """Alert if the next observation is overdue against its own calendar.

    Catches a feed that stops updating without ever raising an exception
    (CONSECUTIVE_ERROR_THRESHOLD only covers raised errors). The boundary
    is the next bar's expected close for this subscription's own timeframe
    and calendar, so a closed market is not mistaken for a dead feed and a
    same-symbol H1 input is not judged on a D1 cadence. Edge-triggered:
    alerts once when crossing into stale and re-arms once fresh data
    resumes. Returns whether the frame is stale so live fails closed.
    """
    status = evaluate_observation(
        latest_ts,
        as_of=as_of,
        timeframe=subscription.timeframe,
        calendar_id=subscription.calendar_id,
        grace=grace,
    )
    if not status.calendar_anchored and subscription not in unanchored_reported:
        unanchored_reported.add(subscription)
        logger.warning(
            "Freshness for %s is not calendar-anchored: %s has no session in %s, "
            "so weekend and holiday awareness is lost for this subscription",
            symbol,
            latest_ts,
            subscription.calendar_id,
        )
    is_stale = not status.fresh
    was_stale = stale_alerted.get(subscription, False)

    if is_stale and not was_stale:
        stale_alerted[subscription] = True
        logger.warning(
            "Stale data: %s latest bar %s, next observation was due %s",
            symbol,
            latest_ts,
            status.due_at,
        )
        notify(
            "send_alert",
            title=f"[{strategy_name}] Stale Data: {symbol}",
            message=(
                f"Latest bar is {latest_ts}; the next observation was due "
                f"{status.due_at} — feed may have stopped updating."
            ),
        )
    elif not is_stale and was_stale:
        stale_alerted[subscription] = False
        logger.info("Stale data recovered: %s", symbol)
    return is_stale


def report_data_readiness(
    missing: Sequence[str],
    *,
    optional_symbols: Container[str],
    alerted: bool,
    notify: Callable[..., None],
    strategy_name: str,
) -> tuple[bool, bool]:
    """Report absent inputs and say whether evaluation must be held.

    Edge-triggered like staleness: one diagnostic when data readiness is
    lost and one when it returns, not one per poll cycle. Optional
    subscriptions are reported and stepped over; required ones hold the
    strategy.

    Returns whether evaluation is blocked, and the caller's new latch value.
    """
    blocking = [symbol for symbol in missing if symbol not in optional_symbols]
    omitted = [symbol for symbol in missing if symbol in optional_symbols]
    if omitted:
        logger.info("Proceeding without optional market data: %s", ", ".join(omitted))
    if blocking and not alerted:
        alerted = True
        logger.warning("Data not ready; holding strategy evaluation for %s", ", ".join(blocking))
        notify(
            "send_alert",
            title=f"[{strategy_name}] Market Data Not Ready",
            message=(
                f"No usable observation for required {', '.join(blocking)}; "
                "strategy evaluation is held until the feed recovers."
            ),
        )
    elif not blocking and alerted:
        alerted = False
        logger.info("Market data ready again; resuming strategy evaluation")
    return bool(blocking), alerted
