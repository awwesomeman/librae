"""Refresh and freshness for auxiliary market-data subscriptions.

An auxiliary is context the strategy reads but never executes on, so this
lane may alert but must never abort a cycle or hold the run. It keeps its
own cache because auxiliaries carry no durable watermark and no fill dedup:
a restart simply refetches them. ``LiveTrader`` binds its own state and
collaborators; nothing here reads the engine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, MutableMapping, MutableSet
from datetime import datetime, timedelta

import pandas as pd

from librae.core.market_data import MarketDataSubscription
from librae.core.readiness import evaluate_observation, next_expected_close

from .interfaces import BarDataFetcher

logger = logging.getLogger(__name__)


def refresh(
    subscriptions: Mapping[MarketDataSubscription, str],
    cache: MutableMapping[MarketDataSubscription, pd.DataFrame],
    *,
    now: datetime,
    fetchers: Mapping[str, BarDataFetcher],
    history_limit: int,
    normalize: Callable[..., pd.DataFrame],
    never_delivered: MutableSet[MarketDataSubscription],
    stale_alerted: MutableMapping[MarketDataSubscription, bool],
    staleness_grace: Callable[[MarketDataSubscription], timedelta],
    notify: Callable[..., None],
    strategy_name: str,
) -> None:
    """Refresh every auxiliary input through its symbol's own source.

    Auxiliaries produce no execution events, so they carry no durable
    watermark and no fill dedup: a restart simply refetches them. Nothing
    here may abort the cycle — the primary still has to execute — so a
    failed fetch, or a frame that fails normalization, logs and keeps the
    previous frame. Freshness is still evaluated and alerted; it just never
    holds the run, because an auxiliary is not an input the run executes on.
    """
    for subscription, owner in subscriptions.items():
        if not _fetch_due(subscription, now, cache=cache):
            continue
        try:
            fetched = fetchers[owner](
                owner,
                subscription.timeframe,
                history_limit + 1,
                drop_incomplete=True,
            )
            if fetched is None or fetched.empty:
                continue
            normalized = normalize(owner, fetched, subscription=subscription)
        except Exception:
            logger.exception(
                "Failed to refresh auxiliary %s %s; keeping the previous frame",
                owner,
                subscription.timeframe,
            )
            continue
        cache[subscription] = normalized

    # Evaluate every declared auxiliary from its cache, whatever the fetch
    # did. Checking only after a successful fetch inspects the feed exactly
    # when it is healthy enough to answer and never when it is not, so the
    # two ordinary ways a feed dies — raising and returning nothing — would
    # age the cache silently while the strategy still reads it as context.
    # This also covers a subscription whose refetch was skipped as not due.
    for subscription, owner in subscriptions.items():
        _check_freshness(
            subscription,
            owner,
            as_of=now,
            cache=cache,
            never_delivered=never_delivered,
            stale_alerted=stale_alerted,
            staleness_grace=staleness_grace,
            notify=notify,
            strategy_name=strategy_name,
        )


def _fetch_due(
    subscription: MarketDataSubscription,
    now: datetime,
    *,
    cache: Mapping[MarketDataSubscription, pd.DataFrame],
) -> bool:
    """Skip a refetch until a new observation could exist.

    A daily auxiliary on an hourly run would otherwise be pulled once per
    poll for a bar that cannot change. The calendar already knows when the
    next one is due.
    """
    cached = cache.get(subscription)
    if cached is None or cached.empty:
        return True
    last_ts = pd.Timestamp(cached["ts"].iloc[-1]).to_pydatetime()
    try:
        return now >= next_expected_close(
            last_ts,
            timeframe=subscription.timeframe,
            calendar_id=subscription.calendar_id,
        )
    except ValueError:
        return True


def _check_freshness(
    subscription: MarketDataSubscription,
    owner: str,
    *,
    as_of: datetime,
    cache: Mapping[MarketDataSubscription, pd.DataFrame],
    never_delivered: MutableSet[MarketDataSubscription],
    stale_alerted: MutableMapping[MarketDataSubscription, bool],
    staleness_grace: Callable[[MarketDataSubscription], timedelta],
    notify: Callable[..., None],
    strategy_name: str,
) -> None:
    """Alert on a stalled auxiliary feed without ever holding the run.

    A dead auxiliary that goes on serving yesterday's frame is the silent
    failure this work exists to close; it just is not grounds to stop
    executing on the primary.

    A subscription that has never delivered is reported once and not
    alerted: there is no observation for it to be late relative to, which
    is the distinction ``ObservationStatus.late`` draws.
    """
    frame = cache.get(subscription)
    if frame is None or frame.empty:
        if subscription not in never_delivered:
            never_delivered.add(subscription)
            logger.warning(
                "Auxiliary %s %s has never delivered an observation",
                owner,
                subscription.timeframe,
            )
        return
    never_delivered.discard(subscription)
    last_ts = pd.Timestamp(frame["ts"].iloc[-1]).to_pydatetime()
    status = evaluate_observation(
        last_ts,
        as_of=as_of,
        timeframe=subscription.timeframe,
        calendar_id=subscription.calendar_id,
        grace=staleness_grace(subscription),
    )
    was_stale = stale_alerted.get(subscription, False)
    if not status.fresh and not was_stale:
        stale_alerted[subscription] = True
        logger.warning(
            "Stale auxiliary data: %s %s latest bar %s, next observation was due %s",
            owner,
            subscription.timeframe,
            last_ts,
            status.due_at,
        )
        notify(
            "send_alert",
            title=(f"[{strategy_name}] Stale Auxiliary Data: {owner} {subscription.timeframe}"),
            message=(
                f"Latest {subscription.timeframe} bar is {last_ts}; the next was due "
                f"{status.due_at}. Strategy execution continues on the primary cadence."
            ),
        )
    elif status.fresh and was_stale:
        stale_alerted[subscription] = False
        logger.info("Auxiliary data recovered: %s %s", owner, subscription.timeframe)
