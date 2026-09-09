"""Deriving session identity for rows the expand migration left null.

The values are derived, never invented: a data source nobody registered a
calendar for is reported and left alone, because guessing one would silently
assign a trading session to nine years of bars.
"""

from __future__ import annotations

from librae.db.backfill import BackfillReport, resolve_calendars


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
