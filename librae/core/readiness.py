"""Calendar-aware freshness for one market-data subscription.

Pure and side-effect free so backtest and live reach the same verdict on the
same fixture — a fixed wall-clock age cannot, because it has no way to know
the venue is closed. Calendars describe where the next observation is
expected; observed samples remain the only market events, and nothing here
invents a bar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from librae.core.trading_calendar import (
    next_segment_open_after,
    period_close,
    period_start,
)
from librae.core.utils import interval_to_timedelta


def _period_close_at_or_after(
    start: datetime,
    *,
    timeframe: str,
    calendar_id: str,
) -> datetime:
    return period_close(start, timeframe, calendar_id).to_pydatetime()


def next_expected_close(
    last_ts: datetime,
    *,
    timeframe: str,
    calendar_id: str,
) -> datetime:
    """When the observation after ``last_ts`` is expected to complete.

    The bar starting at ``last_ts`` completes at its own period close, and the
    next one begins where the calendar says trading resumes. That is what keeps
    a Friday daily bar from looking late all weekend.

    Segments are half-open, so the observation after a segment close belongs
    to the next segment or session. The invariant that holds everywhere is
    that the next close must be *strictly later* than this one; a candidate
    that is not, or that the calendar cannot place, falls through to the
    next segment open.

    Bar timestamps are assumed to be **session-anchored**: a period is stamped
    at the instant it opens, which is what librae's own adapters produce. A
    daily or weekly bar stamped at midnight instead precedes its own session's
    open, so it resolves to that session's close rather than the following
    one — a deadline one period early. Grace absorbs that at daily and weekly
    sizes and no shipped adapter stamps that way, but caller-supplied data that
    does will be held to a slightly stricter deadline than it deserves.

    An observation outside every session — an extended-hours feed under the
    default ``session_mode="extended"`` — anchors on the next segment directly.
    The result is a deadline rather than a prediction, so resolving a
    post-market bar to the next regular segment is deliberately lenient: it
    cannot make a healthy feed look dead.
    """
    try:
        current_close = period_close(last_ts, timeframe, calendar_id).to_pydatetime()
    except ValueError:
        start = next_segment_open_after(last_ts, calendar_id).to_pydatetime()
        return _period_close_at_or_after(start, timeframe=timeframe, calendar_id=calendar_id)

    try:
        candidate_start = period_start(current_close, timeframe, calendar_id).to_pydatetime()
    except ValueError:
        candidate_start = None
    if candidate_start is not None:
        expected = _period_close_at_or_after(
            candidate_start, timeframe=timeframe, calendar_id=calendar_id
        )
        if expected > current_close:
            return expected

    start = next_segment_open_after(current_close, calendar_id).to_pydatetime()
    return _period_close_at_or_after(start, timeframe=timeframe, calendar_id=calendar_id)


@dataclass(frozen=True, slots=True)
class ObservationStatus:
    """Verdict for one subscription at one evaluation instant."""

    fresh: bool
    expected_close: datetime | None
    due_at: datetime | None
    # False when the calendar had no session for the observation, so the
    # boundary came from the bare interval instead. Worth surfacing: it means
    # this subscription is not getting weekend/holiday awareness.
    calendar_anchored: bool = True

    @property
    def late(self) -> bool:
        """Whether a real observation is overdue, as opposed to never seen."""
        return not self.fresh and self.due_at is not None


def evaluate_observation(
    last_ts: datetime | None,
    *,
    as_of: datetime,
    timeframe: str,
    calendar_id: str,
    grace: timedelta,
) -> ObservationStatus:
    """Decide whether a subscription's latest observation is still current.

    ``grace`` is bounded publication slack applied after the expected close,
    not a market-time allowance: it covers a feed that publishes a completed
    bar late, so it stays wall-clock even when the boundary does not.

    A subscription with no observation at all is not fresh, and reports no
    boundary — there is nothing yet to be late relative to, so a caller can
    tell "never arrived" apart from "arrived and then stopped".
    """
    if last_ts is None:
        return ObservationStatus(fresh=False, expected_close=None, due_at=None)
    calendar_anchored = True
    try:
        expected_close = next_expected_close(last_ts, timeframe=timeframe, calendar_id=calendar_id)
    except ValueError:
        # The calendar has no session containing this observation — an
        # extended-session feed, or simply odd data. Degrade to the bare
        # interval rather than raising: staleness monitoring exists to catch a
        # dead feed, and must not itself become a way for a cycle to die. The
        # fallback keeps that detection and only loses calendar awareness.
        calendar_anchored = False
        expected_close = last_ts + 2 * interval_to_timedelta(timeframe)
    due_at = expected_close + grace
    return ObservationStatus(
        fresh=as_of <= due_at,
        expected_close=expected_close,
        due_at=due_at,
        calendar_anchored=calendar_anchored,
    )
