"""Fill the session identity the expand migration left null on existing rows.

`0003` adds ohlcv.calendar_id and ohlcv.available_at as nullable, because a
NOT NULL add would rewrite every chunk of a multi-million-row hypertable
inside one transaction. Reads filter on calendar_id, so until this runs those
rows are invisible rather than merely incomplete.

Values are derived, never invented. An operator's own `symbols` registration
decides the calendar for its data source, and librae's builtin registry is the
fallback; a source whose registrations disagree with each other, or that
neither knows, is reported and skipped rather than guessed. Availability comes
from `period_close`, the same function the writer's completion floor uses.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from librae.core.trading_calendar import ALWAYS_OPEN_CALENDAR, period_close
from librae.core.utils import interval_to_timedelta, to_canonical

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


def fixed_interval_timeframe(timeframe: str) -> bool:
    """Whether a period is a fixed span rather than a calendar-sized one.

    Mirrors the branch `period_close` takes: minute and hour periods are a
    constant width, so on a 24/7 calendar the close is exactly one interval
    on. Day, week and month periods are calendar arithmetic — a month is not
    30 days — and must go through `period_close` itself.
    """
    canonical = to_canonical(timeframe)
    return canonical.startswith(("M", "H")) and not canonical.startswith("MN")


def _fill_always_open(cur, data_source: str, timeframe: str, calendar_id: str) -> int:
    """Fill a fixed-width 24/7 period in SQL, where the close is exact.

    Row by row in Python would mean millions of round trips for a value the
    database can compute in place. Only reached when
    `fixed_interval_timeframe` holds, so the shortcut cannot drift from
    `period_close`.
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
    """Anything the SQL shortcut cannot express, through the engine's own rule.

    `period_close`, not `bar_close`: the writer's completion floor calls the
    former, and they part company on calendar-sized periods. A close computed
    early would make a bar readable before it finished — look-ahead, and
    silent.
    """
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
        pairs = [
            (ts, period_close(ts, timeframe, calendar_id).to_pydatetime()) for ts in timestamps
        ]
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

        if calendar_id == ALWAYS_OPEN_CALENDAR and fixed_interval_timeframe(timeframe):
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
