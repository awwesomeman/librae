"""The schema guard's required columns must exist in the schema it guards.

Every other schema test drives `inspect_schema` through a mocked cursor, so a
required column that no table has still looks compatible there and only fails
against a real database — where it fails closed, blocking writes and live
state on a database that is in fact correct.
"""

from __future__ import annotations

import re
from pathlib import Path

from librae.db.schema import _LEGACY_REQUIRED_COLUMNS

INIT_SQL = Path(__file__).resolve().parents[2] / "librae/db/timescale_init.sql"

_CREATE_TABLE = re.compile(
    r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", re.DOTALL | re.IGNORECASE
)
_COLUMN = re.compile(r"^\s{4}(\w+)\s+\S")
_NOT_A_COLUMN = frozenset({"constraint", "primary", "unique", "foreign", "check"})


def _columns_by_table() -> dict[str, set[str]]:
    sql = INIT_SQL.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}
    for name, body in _CREATE_TABLE.findall(sql):
        columns = {
            match.group(1)
            for line in body.splitlines()
            if (match := _COLUMN.match(line)) and match.group(1).lower() not in _NOT_A_COLUMN
        }
        tables[name] = columns
    return tables


def test_the_parser_reads_every_table_in_the_schema() -> None:
    """Guards the regex itself.

    The body pattern is non-greedy, so one table whose terminator it fails to
    match is swallowed into the previous table's column set — which would let
    a required column be satisfied by a different table entirely, with both
    assertions below still passing. Counting is what catches that; checking
    only that the named tables appear does not.
    """
    sql = INIT_SQL.read_text(encoding="utf-8")
    tables = _columns_by_table()

    assert len(tables) == sql.count("CREATE TABLE")
    assert set(_LEGACY_REQUIRED_COLUMNS) <= set(tables)
    assert "run_id" in tables["backtest_runs"]


def test_every_required_column_exists_in_the_reference_schema() -> None:
    tables = _columns_by_table()

    missing = {
        table: sorted(required - tables[table])
        for table, required in _LEGACY_REQUIRED_COLUMNS.items()
        if required - tables[table]
    }

    assert missing == {}


def test_the_current_revision_marker_is_a_real_column() -> None:
    # `_current_schema_is_compatible` keys the current revision off this one.
    assert "execution_identity" in _columns_by_table()["backtest_runs"]
