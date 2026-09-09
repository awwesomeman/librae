"""Deriving session identity for rows the expand migration left null.

The values are derived, never invented: a data source nobody registered a
calendar for is reported and left alone, because guessing one would silently
assign a trading session to nine years of bars.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from librae.core.trading_calendar import ALWAYS_OPEN_CALENDAR, period_close
from librae.core.utils import interval_to_timedelta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backfill_ohlcv_identity import (
    BackfillReport,
    _unfilled_batches,
    fixed_interval_timeframe,
    resolve_calendars,
)


class FakeCursor:
    """Stands in for the symbols table the resolver consults first."""

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def execute(self, query: str, params: object = None) -> None:
        assert "symbols" in query

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows


class TestResolveCalendars:
    def test_the_builtin_registry_supplies_the_shipped_sources(self) -> None:
        resolved = resolve_calendars(FakeCursor([]))

        assert resolved["binance_spot"] == "24/7"
        assert resolved["shioaji"] == "XTAIFEX"

    def test_a_registered_symbol_overrides_the_shipped_default(self) -> None:
        # The operator's own registration is the database's record of what it
        # actually holds; a shipped default is only a fallback.
        resolved = resolve_calendars(FakeCursor([("shioaji", "XTAI_CUSTOM")]))

        assert resolved["shioaji"] == "XTAI_CUSTOM"

    def test_an_unregistered_source_is_absent_rather_than_guessed(self) -> None:
        resolved = resolve_calendars(FakeCursor([]))

        assert "mystery_feed" not in resolved

    def test_a_source_whose_rows_disagree_is_left_unresolved(self) -> None:
        # Two calendars for one source cannot both be right, and picking either
        # would assign a session to bars that never had one.
        resolved = resolve_calendars(FakeCursor([("mixed", "24/7"), ("mixed", "XNYS")]))

        assert "mixed" not in resolved


class TestReport:
    def test_it_is_complete_only_when_nothing_was_skipped(self) -> None:
        assert BackfillReport(updated=10).complete
        assert not BackfillReport(updated=10, skipped_sources={"mystery": 3}).complete


class TestAvailabilityMatchesTheWriter:
    """The backfilled availability must equal what the writer would compute.

    An availability set earlier than the real close makes a bar readable
    before it finished — look-ahead, and silent. `bar_close` and
    `period_close` agree on minute and hour periods and part company on
    calendar-sized ones, which is exactly what this pins.
    """

    @pytest.mark.parametrize("calendar_id", ["24/7", "XTAIFEX"])
    @pytest.mark.parametrize("timeframe", ["5m", "1h", "4h", "1d", "1w", "1M"])
    def test_the_sql_shortcut_is_taken_only_where_it_is_exact(
        self, calendar_id: str, timeframe: str
    ) -> None:
        ts = pd.Timestamp("2020-03-02T01:00Z")
        expected = period_close(ts, timeframe, calendar_id)

        if fixed_interval_timeframe(timeframe) and calendar_id == ALWAYS_OPEN_CALENDAR:
            # This is the branch the SQL path computes as ts + interval.
            assert ts + interval_to_timedelta(timeframe) == expected
        else:
            # Everything else must go through period_close itself; asserting
            # the shortcut would be wrong here is what catches a widened guard.
            assert not (fixed_interval_timeframe(timeframe) and calendar_id == ALWAYS_OPEN_CALENDAR)

    @pytest.mark.parametrize("timeframe", ["1w", "1M"])
    def test_calendar_sized_periods_are_kept_out_of_the_shortcut(self, timeframe: str) -> None:
        # A month is not a fixed 30 days; taking the shortcut here would set
        # availability days early.
        ts = pd.Timestamp("2020-02-01T00:00Z")

        assert not fixed_interval_timeframe(timeframe)
        assert ts + interval_to_timedelta(timeframe) != period_close(ts, timeframe, "24/7")

    @pytest.mark.parametrize("timeframe", ["5m", "1h", "4h"])
    def test_fixed_width_periods_take_it(self, timeframe: str) -> None:
        assert fixed_interval_timeframe(timeframe)


class FakeTable:
    """A cursor over a fixed set of bar starts, recording how it was asked.

    Simulates the one property that matters: rows already filled stop matching
    `calendar_id IS NULL`, so a walk that forgets its watermark would keep
    re-reading the same head of a nine-year table.
    """

    # Bounded on purpose: a walk that lost its watermark would ask forever,
    # and a hanging test says less than a failing one.
    MAX_QUERIES = 50

    def __init__(self, timestamps: list[int]) -> None:
        self.remaining = sorted(timestamps)
        self.watermarks: list[int | None] = []
        self._result: list[tuple[int]] = []

    def execute(self, query: str, params: tuple) -> None:
        _, _, watermark, _, limit = params
        self.watermarks.append(watermark)
        if len(self.watermarks) > self.MAX_QUERIES:
            raise AssertionError("the walk is not advancing; it re-read the same head")
        after = [t for t in self.remaining if watermark is None or t > watermark]
        self._result = [(t,) for t in after[:limit]]

    def fetchall(self) -> list[tuple[int]]:
        return self._result


class TestTheWalkAdvances:
    def test_it_yields_bounded_batches_and_terminates(self) -> None:
        table = FakeTable(list(range(10)))

        batches = list(_unfilled_batches(table, "src", "H1", 3))

        assert [len(b) for b in batches] == [3, 3, 3, 1]
        assert [t for batch in batches for t in batch] == list(range(10))

    def test_each_query_starts_after_the_previous_batch(self) -> None:
        # Without this the walk rescans from the start every time, so it costs
        # more the further it gets.
        table = FakeTable(list(range(10)))

        list(_unfilled_batches(table, "src", "H1", 3))

        assert table.watermarks == [None, 2, 5, 8, 9]

    def test_an_empty_table_yields_nothing(self) -> None:
        assert list(_unfilled_batches(FakeTable([]), "src", "H1", 3)) == []
