"""Fill the session identity the expand migration left null on existing rows.

`0003` adds ohlcv.calendar_id and ohlcv.available_at as nullable, because a
NOT NULL add would rewrite every chunk of a multi-million-row hypertable
inside one transaction. Reads filter on calendar_id, so until this runs those
rows are invisible rather than merely incomplete.

Values are derived, never invented. The calendar comes from the database's own
`symbols` rows first and librae's builtin registry second; a data source
neither knows is reported and skipped rather than guessed. Availability comes
from `bar_close`, the same function that computes it at write time.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from librae.core.trading_calendar import ALWAYS_OPEN_CALENDAR, bar_close
from librae.core.utils import interval_to_timedelta

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 50_000


@dataclass
class BackfillReport:
    updated: int = 0
    skipped_sources: dict[str, int] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.skipped_sources


def resolve_calendars(cur) -> dict[str, str]:
    """Map each data source to its calendar, preferring the database's record.

    A source the database and the registry both leave unnamed is absent from
    the result; the caller reports it instead of choosing one.
    """
    from librae.config.symbols import _BUILTIN_SYMBOLS

    shipped: dict[str, set[str]] = {}
    for info in _BUILTIN_SYMBOLS.values():
        if info.calendar_id:
            shipped.setdefault(info.data_source, set()).add(info.calendar_id)

    cur.execute(
        "SELECT DISTINCT data_source, calendar_id FROM symbols WHERE calendar_id IS NOT NULL"
    )
    registered: dict[str, set[str]] = {}
    for data_source, calendar_id in cur.fetchall():
        registered.setdefault(str(data_source), set()).add(str(calendar_id))

    # The operator's own registrations win as a whole, but only where they
    # agree: one data source covering two calendars (a broker serving several
    # venues) cannot be filled from the source alone. Accumulate rather than
    # overwrite, or the last row silently decides for all of them.
    resolved: dict[str, str] = {}
    for source in set(shipped) | set(registered):
        candidates = registered.get(source) or shipped.get(source, set())
        if len(candidates) == 1:
            resolved[source] = next(iter(candidates))
    return resolved


def _pending_groups(cur) -> list[tuple[str, str]]:
    cur.execute(
        """SELECT DISTINCT data_source, timeframe
             FROM ohlcv
            WHERE calendar_id IS NULL
            ORDER BY 1, 2"""
    )
    return [(str(source), str(timeframe)) for source, timeframe in cur.fetchall()]


def _fill_always_open(cur, data_source: str, timeframe: str, calendar_id: str) -> int:
    """A 24/7 bar closes exactly one interval after it opens, so SQL is exact.

    Doing these row by row in Python would mean millions of round trips for a
    value the database can compute in place.
    """
    seconds = int(interval_to_timedelta(timeframe).total_seconds())
    cur.execute(
        """UPDATE ohlcv
              SET calendar_id = %s,
                  available_at = ts + make_interval(secs => %s)
            WHERE calendar_id IS NULL AND data_source = %s AND timeframe = %s""",
        (calendar_id, seconds, data_source, timeframe),
    )
    return cur.rowcount


def _fill_by_session(
    cur, data_source: str, timeframe: str, calendar_id: str, batch_size: int
) -> Iterator[int]:
    """Session-aware calendars need the engine's own bar_close per timestamp."""
    seconds = int(interval_to_timedelta(timeframe).total_seconds())
    while True:
        cur.execute(
            """SELECT DISTINCT ts FROM ohlcv
                WHERE calendar_id IS NULL AND data_source = %s AND timeframe = %s
                ORDER BY ts LIMIT %s""",
            (data_source, timeframe, batch_size),
        )
        timestamps = [row[0] for row in cur.fetchall()]
        if not timestamps:
            return
        pairs = [(ts, bar_close(ts, seconds, calendar_id).to_pydatetime()) for ts in timestamps]
        cur.execute(
            """UPDATE ohlcv AS o
                  SET calendar_id = %s, available_at = v.closed_at
                 FROM (SELECT * FROM unnest(%s::timestamptz[], %s::timestamptz[])
                         AS t(bar_ts, closed_at)) AS v
                WHERE o.calendar_id IS NULL AND o.data_source = %s
                  AND o.timeframe = %s AND o.ts = v.bar_ts""",
            (
                calendar_id,
                [ts for ts, _ in pairs],
                [closed for _, closed in pairs],
                data_source,
                timeframe,
            ),
        )
        yield cur.rowcount


def backfill_ohlcv_identity(conn, *, batch_size: int = DEFAULT_BATCH_SIZE) -> BackfillReport:
    """Fill calendar_id and available_at, committing as it goes.

    Resumable by construction: every statement selects on ``calendar_id IS
    NULL``, so an interrupted run leaves committed work in place and repeats
    nothing.
    """
    report = BackfillReport()
    cur = conn.cursor()
    calendars = resolve_calendars(cur)

    for data_source, timeframe in _pending_groups(cur):
        calendar_id = calendars.get(data_source)
        if calendar_id is None:
            cur.execute(
                "SELECT count(*) FROM ohlcv WHERE calendar_id IS NULL AND data_source = %s",
                (data_source,),
            )
            report.skipped_sources[data_source] = int(cur.fetchone()[0])
            continue

        if calendar_id == ALWAYS_OPEN_CALENDAR:
            filled = _fill_always_open(cur, data_source, timeframe, calendar_id)
            conn.commit()
            report.updated += filled
            logger.info("backfilled %d %s %s rows", filled, data_source, timeframe)
            continue

        for filled in _fill_by_session(cur, data_source, timeframe, calendar_id, batch_size):
            conn.commit()
            report.updated += filled
            logger.info("backfilled %d %s %s rows", filled, data_source, timeframe)
    return report
