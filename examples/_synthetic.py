"""Shared helpers for calendar-aligned synthetic example data."""

from __future__ import annotations

import exchange_calendars as xcals
import pandas as pd


def session_opens(
    calendar_id: str,
    *,
    start: str,
    periods: int,
) -> pd.DatetimeIndex:
    """Return consecutive canonical session opens for an exchange calendar."""
    if periods <= 0:
        raise ValueError("periods must be positive")

    calendar = xcals.get_calendar(calendar_id)
    first_session = calendar.date_to_session(pd.Timestamp(start), direction="next")
    sessions = calendar.sessions_window(first_session, periods)
    return pd.DatetimeIndex(calendar.schedule.loc[sessions, "open"])
