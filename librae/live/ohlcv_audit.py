"""Delivery of one event's OHLCV audit rows to the analytics sink.

Two contracts live here. A sink that declares ``durable_ohlcv_delivery``
gets at-least-once semantics through a bounded queue that rides the restart
checkpoint; any other callback keeps the documented best-effort contract.
The queue is a plain list owned by ``LiveTrader`` and truncated in place, so
the checkpoint written after a drain sees what was actually acknowledged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import pandas as pd

from librae.core.executor import RuntimeEvent
from librae.core.market_data import AVAILABLE_AT_COLUMN, MarketDataSubscription

from .interfaces import OhlcvCallback, RuntimeEventCallback
from .state import MAX_PENDING_OHLCV, PendingOhlcvDelivery

logger = logging.getLogger(__name__)


def deliver(
    audit_bars: Mapping[str, dict[str, object]],
    ts: datetime,
    *,
    on_ohlcv: OhlcvCallback | None,
    durable: bool,
    timeframe: str,
    flush: Callable[[], None],
) -> None:
    """Hand one event's audit rows to the OHLCV sink.

    A sink that declares ``durable_ohlcv_delivery`` gets at-least-once
    semantics: the row is queued in the checkpoint before it is offered,
    so a failed write or a crash replays it rather than losing it. The
    writer treats an equal row version as an idempotent no-op, which is
    what makes a duplicate delivery harmless.

    Any other callback keeps the documented best-effort contract. Queueing
    its failures would grow the checkpoint for work the engine has no way
    to acknowledge.
    """
    if on_ohlcv is None:
        return
    if not durable:
        for symbol, bar in audit_bars.items():
            try:
                on_ohlcv(symbol, timeframe, bar, ts)
            except Exception:
                logger.exception("Best-effort OHLCV callback failed for %s at %s", symbol, ts)
        return
    flush()


def queue(
    audit_bars: Mapping[str, dict[str, object]],
    ts: datetime,
    pending: list[PendingOhlcvDelivery],
    *,
    on_ohlcv: OhlcvCallback | None,
    durable: bool,
    subscriptions: Mapping[str, MarketDataSubscription],
    degraded: bool,
    utc_now: Callable[[], datetime],
    on_runtime_event: RuntimeEventCallback | None,
    notify: Callable[..., None],
    strategy_name: str,
) -> bool:
    """Accept this event's audit rows, before the checkpoint that lands
    the watermark they belong to.

    Called immediately before that checkpoint rather than after it, so one
    write carries both. Landing them separately would double the
    round-trips per bar for no extra guarantee.

    Returns the caller's new audit-degraded latch.
    """
    if on_ohlcv is None or not durable:
        return degraded
    for symbol, bar in audit_bars.items():
        degraded = _enqueue(
            bar,
            ts,
            pending,
            subscription=subscriptions[symbol],
            degraded=degraded,
            utc_now=utc_now,
            on_runtime_event=on_runtime_event,
            notify=notify,
            strategy_name=strategy_name,
        )
    return degraded


def _enqueue(
    bar: Mapping[str, object],
    ts: datetime,
    pending: list[PendingOhlcvDelivery],
    *,
    subscription: MarketDataSubscription,
    degraded: bool,
    utc_now: Callable[[], datetime],
    on_runtime_event: RuntimeEventCallback | None,
    notify: Callable[..., None],
    strategy_name: str,
) -> bool:
    """Record an accepted row before the watermark can forget it."""
    available_at = bar.get(AVAILABLE_AT_COLUMN)
    row = PendingOhlcvDelivery(
        subscription=subscription,
        ts=ts,
        available_at=(
            pd.Timestamp(available_at).to_pydatetime().astimezone(UTC)
            if available_at is not None
            else ts
        ),
        bar={
            field: float(bar[field])
            for field in ("open", "high", "low", "close", "volume")
            if field in bar
        },
    )
    if len(pending) >= MAX_PENDING_OHLCV:
        return _drop_audit_row(
            row,
            degraded=degraded,
            utc_now=utc_now,
            on_runtime_event=on_runtime_event,
            notify=notify,
            strategy_name=strategy_name,
        )
    pending.append(row)
    return degraded


def _drop_audit_row(
    row: PendingOhlcvDelivery,
    *,
    degraded: bool,
    utc_now: Callable[[], datetime],
    on_runtime_event: RuntimeEventCallback | None,
    notify: Callable[..., None],
    strategy_name: str,
) -> bool:
    """Drop one audit row loudly, without stopping the book.

    This queue holds OHLCV bars, the most recoverable data in the system:
    a gap is closed by re-fetching from the source, and nothing in the
    book depends on it. Halting cancels live orders and stops trading,
    which has real market cost, to protect data that can be rebuilt.

    Reaching the bound is not an outage either. The checkpoint and these
    rows share one database, so a database refusing this many OHLCV writes
    while still accepting checkpoints is a schema or constraint problem.
    It surfaces as terminal health and a recorded identity, so a backfill
    knows what to close.

    Returns the caller's new audit-degraded latch.
    """
    if on_runtime_event:
        on_runtime_event(
            RuntimeEvent(
                ts=utc_now(),
                event_type="decision_skipped",
                symbol=row.subscription.symbol,
                detail={
                    "reason": "ohlcv_audit_delivery_failed",
                    "timeframe": row.subscription.timeframe,
                    "ts": row.ts.isoformat(),
                    "available_at": row.available_at.isoformat(),
                    "pending_bound": MAX_PENDING_OHLCV,
                },
            )
        )
    if not degraded:
        degraded = True
        logger.error(
            "OHLCV audit backlog reached %d unacknowledged rows; further rows are "
            "dropped and must be backfilled from the data source",
            MAX_PENDING_OHLCV,
        )
        notify(
            "send_alert",
            title=f"[{strategy_name}] OHLCV Audit Backlog",
            message=(
                f"{MAX_PENDING_OHLCV} unacknowledged audit rows; further rows are "
                "dropped and recorded individually. Trading continues — these bars "
                "are re-fetchable and the book does not depend on them."
            ),
        )
    return degraded


def flush_pending(
    pending: list[PendingOhlcvDelivery],
    *,
    on_ohlcv: OhlcvCallback | None,
    degraded: bool,
    persist: Callable[[], None],
) -> bool:
    """Offer queued rows in order, keeping whatever is not acknowledged.

    Order matters per subscription: the writer accepts only a strictly
    later row version, so replaying a correction before the bar it
    corrects would drop it.

    Acknowledged rows leave ``pending`` in place, before ``persist`` runs,
    so the checkpoint records the queue the sink actually accepted.

    Returns the caller's new audit-degraded latch.
    """
    if not pending or on_ohlcv is None:
        return degraded
    delivered = 0
    for row in list(pending):
        bar = dict(row.bar)
        bar[AVAILABLE_AT_COLUMN] = row.available_at
        try:
            on_ohlcv(row.subscription.symbol, row.subscription.timeframe, bar, row.ts)
        except Exception:
            logger.warning(
                "OHLCV audit delivery failed for %s %s at %s; %d row(s) still pending",
                row.subscription.symbol,
                row.subscription.timeframe,
                row.ts,
                len(pending) - delivered,
            )
            break
        delivered += 1
    if delivered:
        del pending[:delivered]
        persist()
    if degraded and not pending:
        degraded = False
        logger.info("OHLCV audit backlog cleared; delivery has caught up")
    return degraded
