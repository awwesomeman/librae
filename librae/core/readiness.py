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
    next_session_open,
    period_close,
    period_start,
    session_label,
)
from librae.core.utils import interval_to_timedelta


def next_expected_close(
    last_ts: datetime,
    *,
    timeframe: str,
    calendar_id: str,
) -> datetime:
    """When the observation after ``last_ts`` is expected to complete.

    The bar starting at ``last_ts`` completes at its own period close. The
    next one starts there when the session still has room, and at the next
    session's open when it does not — which is what keeps a Friday daily bar
    from looking late all weekend.
    """
    current_close = period_close(last_ts, timeframe, calendar_id)
    try:
        next_start = period_start(current_close, timeframe, calendar_id)
    except ValueError:
        # The session ended on this boundary, so the next observation belongs
        # to the following session rather than to a gap in this one.
        next_start = next_session_open(session_label(last_ts, calendar_id), calendar_id)
    return period_close(next_start, timeframe, calendar_id).to_pydatetime()


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
