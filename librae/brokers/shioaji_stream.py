"""Minute bars for a live Shioaji poll, built from its tick subscription.

Shioaji's ``kbars()`` serves after-hours analysis: it is capped at 270
queries during the session, and live data is to come from the quote
subscription, which costs no traffic
(https://sinotrade.github.io/zh/tutor/limit/). So a live poll is answered
from subscribed ticks, aggregated into the minutes ``kbars()`` would return,
and ``kbars()`` settles only the minutes the stream cannot vouch for. This is
NautilusTrader's request-then-subscribe split for bars
(https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/data/index.md),
and Shioaji's quote manager tutorial, whose tick callback only appends and
whose reader aggregates
(https://sinotrade.github.io/zh/tutor/advanced/quote_manager_basic/).

A trade belongs to the minute it happened in, start included and end
excluded; a print in the minute after a segment closes (TAIFEX's closing
auction, stamped 13:45:00.024 on 2026-11-05) belongs to the segment's last
minute. Built that way from historical ticks, TXFR1 for the trading days
2026-11-04 and 2026-11-05 and MXFR1 for 2026-11-05, all 3,414 minutes equal
``kbars()``.

A tick whose cumulative volume follows the previous tick's shows that no
trade between them was missed. ``kbars()`` settles what that cannot show:
the minutes before the subscription, the minutes around a tick that does not
follow on or a lost connection, and each segment's last minutes, which no
later tick of the segment follows. Until then a minute is not served as
complete. NautilusTrader's order-book recovery treats a sequence gap the same
way: stop emitting, recover once from a snapshot, resume
(https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/developer_guide/adapters.md).
A trade missed after a thin contract's last tick shows only at its next
tick, so its minute can be released short and is then revised.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from librae.core.trading_calendar import next_segment_open_after, period_start, segment_closes

from .base import SEGMENT_END_SETTLE

logger = logging.getLogger(__name__)

_TAIPEI = "Asia/Taipei"
_MINUTE_NS = 60 * 1_000_000_000
_TAIPEI_UTC_OFFSET_NS = 8 * 60 * _MINUTE_NS
_SETTLE_NS = SEGMENT_END_SETTLE.value
_EPOCH = datetime(1970, 1, 1)
_MICROSECOND = timedelta(microseconds=1)

# The stream is taken to hold every trade from the first minute starting this
# long after the subscription is acknowledged by the host clock (assumed, not
# measured). kbars() settles that minute before any streamed one is served,
# so only a subscription starting a minute late would matter.
_SUBSCRIBE_MARGIN_NS = 2 * 1_000_000_000

# kbars() refills, and retries of a failed seed, of one contract come at most
# this often, so a stream that keeps breaking costs a few queries an hour.
_QUERY_SPACING_NS = 5 * _MINUTE_NS

# kbars() queries the streams of one session may spend in a Taipei day.
# Shioaji allows 270 during the session for everything the account runs, a
# scheduled collection included, and exceeding limits repeatedly can suspend
# the account (https://sinotrade.github.io/zh/tutor/limit/).
_DAILY_KBARS_BUDGET = 100

# Ticks waiting for the next poll. A full queue drops the oldest, which the
# volume check then reports as a gap.
_INBOX_SIZE = 200_000

# Solace session events Shioaji forwards
# (https://sinotrade.github.io/zh/tutor/callback/event_cb/): the session went
# down, failed to connect, or is reconnecting; and a refused subscription.
_CONNECTION_LOST_EVENTS = frozenset({1, 2, 12})
_SUBSCRIPTION_REFUSED_EVENT = 4

_COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _now_ns() -> int:
    return pd.Timestamp.now(tz=UTC).value


def _tick_ns(at: datetime) -> int:
    """UTC epoch nanoseconds of a tick's naive Taipei wall-clock time."""
    return (at - _EPOCH) // _MICROSECOND * 1000 - _TAIPEI_UTC_OFFSET_NS


def _first_minute_stamped(day: date) -> int:
    """Start of the first minute ``kbars()`` dates ``day``: it dates a minute
    by its end, so that is the minute ending at midnight."""
    return pd.Timestamp(day, tz=_TAIPEI).value - _MINUTE_NS


def _next_minute_after(at_ns: int) -> int:
    return at_ns - at_ns % _MINUTE_NS + _MINUTE_NS


class _MinuteBook:
    """One contract's minutes: settled ones from ``kbars()``, then streamed ones."""

    def __init__(self, stream_code: str, calendar_id: str) -> None:
        self.stream_code = stream_code
        self.calendar_id = calendar_id
        self.first_day: date | None = None
        # Minute start (UTC ns) -> (open, high, low, close, volume). Every
        # minute before final_before is complete; later ones may change.
        self.history: dict[int, tuple[float, ...]] = {}
        self.final_before: int | None = None
        self.last_refill_ns: int | None = None
        self.seed_retry_ns: int | None = None  # set while a seed is owed
        # Minutes built from ticks. The stream holds every trade from
        # covered_from on; None while it holds none for certain.
        self.minutes: dict[int, list[float]] = {}
        self.covered_from: int | None = None
        self.last_total: int | None = None
        self.last_minute: int | None = None
        self.segment: tuple[int, int] | None = None  # (a minute in it, its close)
        self.ended_segment: int | None = None  # close of the last segment handed to kbars()

    # -- ticks ---------------------------------------------------------------

    def start_stream(self, from_ns: int) -> None:
        self.covered_from = from_ns
        self.last_total = None

    def _minute_of(self, at_ns: int) -> tuple[int | None, bool]:
        """The kbars() minute a trade at ``at_ns`` counts in, and whether it
        is the first of its trading segment here (having ended the last)."""
        minute = at_ns - at_ns % _MINUTE_NS
        if self.segment is not None:
            seen, close = self.segment
            if seen <= minute < close:
                return minute, False
            if close <= at_ns < close + _MINUTE_NS:
                return close - _MINUTE_NS, False
        # The minute itself, or for a closing print the one before it.
        for candidate in (minute, minute - _MINUTE_NS):
            start = pd.Timestamp(candidate, tz=UTC)
            try:
                period_start(start, "1m", self.calendar_id)
            except ValueError:
                continue
            close = segment_closes(pd.DatetimeIndex([start]), "1m", self.calendar_id)[0].value
            new_segment = self.segment is None or self.segment[1] != close
            if new_segment and self.segment is not None:
                self._end_segment(self.segment[1])
            self.segment = (candidate, close)
            return candidate, new_segment
        return None, False

    def add_tick(self, at_ns: int, price: float, volume: int, total: int) -> None:
        minute, new_segment = self._minute_of(at_ns)
        if minute is None:
            logger.warning(
                "Shioaji %s traded at %s, outside the %s sessions; tick ignored",
                self.stream_code,
                pd.Timestamp(at_ns, tz=UTC).isoformat(),
                self.calendar_id,
            )
            return
        if self.last_total is not None:
            if new_segment:
                # Whether a segment restarts the cumulative volume or carries
                # it on, its first tick is one of these.
                continuous = total in (volume, self.last_total + volume)
            elif total <= self.last_total:
                return  # delivered before
            else:
                continuous = total == self.last_total + volume
            if not continuous:
                logger.warning(
                    "Shioaji %s cumulative volume went from %d to %d on a %d-lot tick "
                    "at %s: trades are missing, so its minutes from %s wait for kbars()",
                    self.stream_code,
                    self.last_total,
                    total,
                    volume,
                    pd.Timestamp(at_ns, tz=UTC).isoformat(),
                    pd.Timestamp(
                        minute if self.last_minute is None else self.last_minute, tz=UTC
                    ).isoformat(),
                )
                self._settle_streamed()
                self.covered_from = minute + _MINUTE_NS
        self.last_total = total
        self.last_minute = minute
        row = self.minutes.get(minute)
        if row is None:
            self.minutes[minute] = [price, price, price, price, float(volume)]
        else:
            row[1] = max(row[1], price)
            row[2] = min(row[2], price)
            row[3] = price
            row[4] += volume

    def end_segment(self, now_ns: int) -> None:
        if self.segment is not None and now_ns >= self.segment[1]:
            self._end_segment(self.segment[1])

    def _end_segment(self, close: int) -> None:
        """Hand a closed segment's last minutes to kbars(): a trade missed
        after its last tick would show at no later tick of the segment, and
        the next segment may restart the cumulative volume."""
        if self.ended_segment == close:
            return
        self.ended_segment = close
        if self.covered_from is None:
            return  # every streamed minute already waits for kbars()
        self._settle_streamed()
        self.covered_from = max(self.covered_from, close)

    def connection_lost(self) -> None:
        self._settle_streamed()
        self.covered_from = None
        self.last_total = None

    def _settle_streamed(self) -> None:
        """Make final the streamed minutes before the last tick's: the stream
        held every trade in them, so kbars() has nothing to add."""
        if (
            self.covered_from is None
            or self.final_before is None
            or self.covered_from > self.final_before
            or self.last_minute is None
            or self.last_minute <= self.final_before
        ):
            return
        start, end = self.final_before, self.last_minute
        for minute in [m for m in self.history if start <= m < end]:
            del self.history[minute]
        for minute, row in self.minutes.items():
            if start <= minute < end:
                self.history[minute] = tuple(row)
        self.final_before = end

    # -- kbars ---------------------------------------------------------------

    def holds_gap(self) -> bool:
        return (
            self.final_before is None
            or self.covered_from is None
            or self.final_before < self.covered_from
        )

    def skip_closed_market(self) -> None:
        """Close a gap that no session minute falls in: nothing traded there."""
        if not self.holds_gap() or self.final_before is None or self.covered_from is None:
            return
        start = pd.Timestamp(self.final_before, tz=UTC)
        try:
            period_start(start, "1m", self.calendar_id)
            return
        except ValueError:
            pass
        if next_segment_open_after(start, self.calendar_id).value >= self.covered_from:
            self.final_before = self.covered_from

    def take_kbars(self, minutes: pd.DataFrame, first_day: date, queried_ns: int) -> None:
        """Replace the settled minutes with one ``kbars()`` answer from ``first_day``.

        A minute is final once a later one is in the answer, or once it
        closed ``SEGMENT_END_SETTLE`` before the query (see
        ``drop_incomplete_ohlcv``), unless the stream holds trades in a
        later minute the answer lacks: then kbars() is running late.
        """
        if minutes.empty and (self.history or self.minutes):
            # Shioaji answers empty once the daily traffic cap is spent
            # (https://sinotrade.github.io/zh/tutor/limit/). Trades already
            # seen in the window show this answer is not that nothing traded.
            logger.warning(
                "Shioaji kbars() for %s came back empty; its unsettled minutes stay so",
                self.stream_code,
            )
            return
        starts = pd.DatetimeIndex(minutes["ts"]).as_unit("ns").asi8.tolist() if len(minutes) else []
        values = minutes[["open", "high", "low", "close", "volume"]].itertuples(
            index=False, name=None
        )
        answer = {start: tuple(map(float, row)) for start, row in zip(starts, values, strict=True)}
        newest = max(starts, default=None)
        settled = queried_ns - _SETTLE_NS
        final_before = settled - settled % _MINUTE_NS
        if newest is not None and any(
            newest < m < final_before
            for m in self.minutes
            if self.covered_from is not None and m >= self.covered_from
        ):
            # The stream holds trades in a minute the answer lacks, so
            # kbars() is running late: only the minutes before its newest are
            # final, and settled ones after that stay as they were.
            logger.warning(
                "Shioaji kbars() for %s ends at %s, before trades the stream holds; "
                "its later minutes wait for the next refill",
                self.stream_code,
                pd.Timestamp(newest, tz=UTC).isoformat(),
            )
            kept = self.final_before if self.final_before is not None else newest
            answer = {m: row for m, row in answer.items() if m < newest}
            answer.update((m, row) for m, row in self.history.items() if newest <= m < kept)
            final_before = max(newest, kept)
        elif newest is not None:
            final_before = max(final_before, newest)
        self.history = answer
        self.final_before = final_before
        self.first_day = first_day
        self.minutes = {m: row for m, row in self.minutes.items() if m >= final_before}

    # -- reading -------------------------------------------------------------

    def frame(self, first_day: date, *, complete_only: bool) -> tuple[pd.DataFrame, int | None]:
        """Minutes stamped from ``first_day`` on, and the start before which
        every one is complete (None: all but the one still forming)."""
        if self.final_before is None:
            raise RuntimeError(f"Shioaji {self.stream_code} minutes read before kbars() seeded")
        if self.first_day is None or first_day > self.first_day:
            first = _first_minute_stamped(first_day)
            for book in (self.history, self.minutes):
                for minute in [m for m in book if m < first]:
                    del book[minute]
            self.first_day = first_day
        settled = self.final_before
        rows = {m: row for m, row in self.history.items() if m < settled}
        complete_through = settled if self.holds_gap() else None
        if complete_through is None:
            rows.update((m, row) for m, row in self.minutes.items() if m >= settled)
        elif not complete_only:
            # What is known so far of the minutes still to be settled.
            rows.update((m, row) for m, row in self.history.items() if m >= settled)
            rows.update((m, row) for m, row in self.minutes.items() if m >= settled)
        frame = pd.DataFrame(sorted((m, *row) for m, row in rows.items()), columns=_COLUMNS)
        frame["ts"] = pd.to_datetime(frame["ts"], unit="ns", utc=True)
        return frame, complete_through


class ShioajiBarStream:
    """Tick subscriptions and minute books for one Shioaji session."""

    def __init__(self, api: Any, query_minutes: Callable[[Any, date, date], pd.DataFrame]) -> None:
        self._api = api
        self._query_minutes = query_minutes
        self._lock = threading.Lock()
        self._inbox: deque[tuple] = deque(maxlen=_INBOX_SIZE)
        self._books: dict[str, _MinuteBook] = {}
        self._subscribed: dict[str, Any] = {}  # stream code -> contract it was asked with
        self._lost: set[str] = set()  # stream codes whose subscription a lost connection ended
        self._callbacks_set = False
        self._budget_day: date | None = None
        self._queries_today = 0

    # -- SDK callbacks: they only append, as Shioaji asks of quote callbacks.
    # SDK 1.7.10 dropped an exception raised in one without a trace on
    # 2026-11-05.

    def _on_tick(self, *args: Any) -> None:
        # The streaming tutorial passes (exchange, tick); the SDK stub
        # declares the tick alone, which is what SDK 1.7.10 passed.
        tick = args[-1]
        self._inbox.append(
            (
                "tick",
                tick.code,
                tick.datetime,
                tick.close,
                tick.volume,
                tick.total_volume,
                tick.simtrade,
            )
        )

    def _on_event(self, _resp_code: int, event_code: int, info: str, event: str) -> None:
        if event_code in _CONNECTION_LOST_EVENTS:
            self._inbox.append(("lost", event))
        elif event_code == _SUBSCRIPTION_REFUSED_EVENT:
            self._inbox.append(("refused", f"{event} {info}"))

    def _on_session_down(self) -> None:
        self._inbox.append(("lost", "session down"))

    # -- polling -------------------------------------------------------------

    def minutes(
        self,
        contract: Any,
        first_day: date,
        last_day: date,
        calendar_id: str,
        *,
        complete_only: bool,
    ) -> tuple[pd.DataFrame, int | None]:
        """Minutes of ``contract`` stamped ``first_day`` to ``last_day``.

        Returns them start-stamped in UTC, as ``shioaji_kbar_starts`` leaves
        ``kbars()`` minutes, with the start before which every one is
        complete (see ``_MinuteBook.frame``).
        """
        stream_code = str(getattr(contract, "target_code", "") or contract.code)
        with self._lock:
            self._drain()
            book = self._book(contract, stream_code, calendar_id)
            if stream_code not in self._subscribed:
                self._subscribe(contract, stream_code)
            now = _now_ns()
            book.end_segment(now)
            seed = book.final_before is None or book.first_day is None or first_day < book.first_day
            if seed and book.seed_retry_ns is not None and now < book.seed_retry_ns:
                raise RuntimeError(
                    f"Shioaji kbars() has not seeded {stream_code}'s minutes from {first_day}; "
                    f"it is asked again from {pd.Timestamp(book.seed_retry_ns, tz=UTC)}"
                )
            if not seed:
                book.skip_closed_market()
            due = (
                book.holds_gap()
                and book.covered_from is not None
                and now >= book.covered_from + _SETTLE_NS
                and (book.last_refill_ns is None or now >= book.last_refill_ns + _QUERY_SPACING_NS)
            )
            if seed or due:
                query_from = first_day if seed else book.first_day
                self._spend_query(stream_code)
                if seed:
                    book.seed_retry_ns = now + _QUERY_SPACING_NS
                else:
                    book.last_refill_ns = now
                book.take_kbars(
                    self._query_minutes(contract, query_from, last_day), query_from, now
                )
                if seed and book.first_day == query_from:
                    book.seed_retry_ns = None
                book.skip_closed_market()
                logger.info(
                    "Shioaji kbars() %s %s; %s",
                    "seeded" if seed else "refilled",
                    stream_code,
                    f"minutes from {pd.Timestamp(book.final_before, tz=UTC).isoformat()} "
                    "wait for the next refill"
                    if book.holds_gap()
                    else "the stream carries it on",
                )
            return book.frame(first_day, complete_only=complete_only)

    def _book(self, contract: Any, stream_code: str, calendar_id: str) -> _MinuteBook:
        code = str(contract.code)
        book = self._books.get(code)
        if book is not None and book.stream_code == stream_code and book.calendar_id == calendar_id:
            return book
        if book is not None:
            # A rolling alias moved to its next contract; its kbars() and its
            # ticks come from that one now.
            logger.info("Shioaji %s follows %s instead of %s", code, stream_code, book.stream_code)
            del self._books[code]
            self._release(book.stream_code)
        book = _MinuteBook(stream_code, calendar_id)
        self._books[code] = book
        if stream_code in self._subscribed:
            book.start_stream(_next_minute_after(_now_ns() + _SUBSCRIBE_MARGIN_NS))
        return book

    def _release(self, stream_code: str) -> None:
        if stream_code not in self._subscribed or any(
            book.stream_code == stream_code for book in self._books.values()
        ):
            return
        self._api.unsubscribe(self._subscribed.pop(stream_code), quote_type="tick")

    def _subscribe(self, contract: Any, stream_code: str) -> None:
        if not self._callbacks_set:
            self._api.set_on_tick_fop_v1_callback(self._on_tick)
            self._api.set_event_callback(self._on_event)
            self._api.set_session_down_callback(self._on_session_down)
            self._callbacks_set = True
        if stream_code in self._lost:
            # A reconnected session may have kept the subscription or not;
            # replacing it leaves exactly one.
            try:
                self._api.unsubscribe(contract, quote_type="tick")
            except Exception:
                logger.info("Shioaji %s had no tick subscription left to replace", stream_code)
        self._api.subscribe(contract, quote_type="tick")
        self._lost.discard(stream_code)
        self._subscribed[stream_code] = contract
        covered_from = _next_minute_after(_now_ns() + _SUBSCRIBE_MARGIN_NS)
        for book in self._books.values():
            if book.stream_code == stream_code:
                book.start_stream(covered_from)
        logger.info(
            "Shioaji subscribed to %s ticks; streamed minutes from %s",
            stream_code,
            pd.Timestamp(covered_from, tz=UTC).isoformat(),
        )

    def _drain(self) -> None:
        books_by_code: dict[str, list[_MinuteBook]] = {}
        for book in self._books.values():
            books_by_code.setdefault(book.stream_code, []).append(book)
        while self._inbox:
            item = self._inbox.popleft()
            if item[0] == "lost":
                logger.warning(
                    "Shioaji quote connection lost (%s); streamed minutes wait for kbars() "
                    "once the subscriptions are renewed",
                    item[1],
                )
                self._lost.update(self._subscribed)
                self._subscribed.clear()
                for book in self._books.values():
                    book.connection_lost()
                continue
            if item[0] == "refused":
                logger.warning("Shioaji refused a quote subscription change: %s", item[1])
                continue
            _, code, at, close, volume, total, simtrade = item
            if simtrade:
                continue  # pre-open trial matching, not a trade
            if code not in self._subscribed:
                continue
            for book in books_by_code.get(code, ()):
                book.add_tick(_tick_ns(at), float(close), int(volume), int(total))

    def _spend_query(self, stream_code: str) -> None:
        today = pd.Timestamp.now(tz=_TAIPEI).date()
        if self._budget_day != today:
            self._budget_day = today
            self._queries_today = 0
        if self._queries_today >= _DAILY_KBARS_BUDGET:
            raise RuntimeError(
                f"Shioaji bar stream has spent its {_DAILY_KBARS_BUDGET} kbars() queries for "
                f"{today}; {stream_code} needs another to settle its minutes, so its bars "
                "stop here rather than come out incomplete"
            )
        self._queries_today += 1
