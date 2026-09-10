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


def test_the_stranded_warning_resolves_without_the_repository() -> None:
    """The wheel ships `librae*` only — no docs/, no scripts/.

    This warning reaches someone who installed the package and has an older
    database, so a bare repository path tells them to open a file they do not
    have. Twice it named one, and once it named nothing at all — a silent
    warning about a silent gap. A URL is what resolves for both readers.
    """
    from librae.db.schema import _STRANDED_MESSAGE

    references = re.findall(r"\S*/\S*", _STRANDED_MESSAGE)

    # Both halves matter, and both have already failed once. A message with no
    # pointer at all passes a loop over an empty list, and that is exactly what
    # an earlier revision of this warning said: a gap exists, with no remedy.
    assert references, "the warning must point somewhere"
    for reference in references:
        assert reference.startswith("https://"), reference


MIGRATION_0003 = INIT_SQL.parent / "migrations/0003_adopt_subscription_identity.sql"


def test_the_view_acl_is_captured_before_the_drop_and_replayed_after() -> None:
    """Dropping a view drops every ACL on it, not just librae's own.

    A deployment may have granted `data_inventory` to a BI account or a read
    replica; those grants survive only if the ACL is read before the drop and
    replayed after it.
    """
    sql = MIGRATION_0003.read_text(encoding="utf-8")
    capture = sql.index("CREATE TEMP TABLE data_inventory_acl")
    drop = sql.index("DROP VIEW IF EXISTS data_inventory")

    assert capture < drop
    assert "FROM data_inventory_acl" in sql[drop:], "the captured ACL is never replayed"
