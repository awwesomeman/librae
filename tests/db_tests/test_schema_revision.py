"""The fail-closed schema revision gate and the bootstrap that stamps it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from librae.db.schema import CURRENT_SCHEMA_REVISION, require_current_schema

BOOTSTRAP = Path(__file__).resolve().parents[2] / "librae/db/timescale_init.sql"

CURRENT_SCHEMA_PROBE = (
    "SELECT to_regclass(quote_ident(current_schema()) || '.librae_schema_revision')"
)
REVISION_READ = "SELECT revision FROM librae_schema_revision WHERE singleton = TRUE"


class FakeCursor:
    """Answers only the reads the gate may make; quant_app is granted no more.

    ``copies`` maps each schema holding a revision table to its revision row
    (``None`` for an empty table). Names resolve as PostgreSQL resolves them:
    a bare name takes the first schema on ``search_path`` that has it, and
    ``current_schema()`` is the first schema on ``search_path``.
    """

    def __init__(
        self,
        revision: int | None,
        *,
        has_table: bool = True,
        search_path: tuple[str, ...] = ("public",),
        copies: dict[str, int | None] | None = None,
    ) -> None:
        self.search_path = search_path
        if copies is None:
            copies = {search_path[0]: revision} if has_table else {}
        self.copies = copies
        self._row: tuple[object, ...] | None = None
        self.queries: list[str] = []

    def _resolve(self, path: tuple[str, ...]) -> str | None:
        return next((schema for schema in path if schema in self.copies), None)

    def execute(self, query: str, params: object = None) -> None:
        self.queries.append(query)
        if query == CURRENT_SCHEMA_PROBE:
            schema = self._resolve(self.search_path[:1])
            self._row = (None if schema is None else f"{schema}.librae_schema_revision",)
        elif query == REVISION_READ:
            schema = self._resolve(self.search_path)
            assert schema is not None, "relation librae_schema_revision does not exist"
            revision = self.copies[schema]
            self._row = None if revision is None else (revision,)
        else:
            raise AssertionError(f"unexpected query: {query}")

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


def _refusal(cursor: FakeCursor) -> str:
    with pytest.raises(RuntimeError, match="incompatible Librae database schema") as excinfo:
        require_current_schema(cursor)
    message = str(excinfo.value)
    assert "timescale_init.sql" in message
    assert "deployment" in message
    assert f"revision {CURRENT_SCHEMA_REVISION}" in message
    return message


def test_current_revision_passes() -> None:
    require_current_schema(FakeCursor(CURRENT_SCHEMA_REVISION))


def test_missing_revision_table_fails_closed() -> None:
    """Covers both an empty and an unversioned database."""
    assert "librae_schema_revision" in _refusal(FakeCursor(None, has_table=False))


@pytest.mark.parametrize("revision", [CURRENT_SCHEMA_REVISION - 1, CURRENT_SCHEMA_REVISION + 1])
def test_any_other_revision_fails_closed(revision: int) -> None:
    assert f"is at revision {revision}" in _refusal(FakeCursor(revision))


def test_a_revision_table_without_its_row_fails_closed() -> None:
    assert "has no revision row" in _refusal(FakeCursor(None))


def test_bootstrap_is_current_and_does_not_embed_upgrade_ddl() -> None:
    schema = BOOTSTRAP.read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS librae_schema_revision" in schema
    assert "ALTER TABLE" not in schema
    assert "BEGIN;" in schema
    assert schema.rstrip().endswith("COMMIT;")


def test_bootstrap_states_one_revision_everywhere_it_is_written() -> None:
    """The bootstrap stamps, verifies, and names its revision in separate
    statements; a bump that misses one would refuse every fresh database."""
    schema = BOOTSTRAP.read_text(encoding="utf-8")
    revision = CURRENT_SCHEMA_REVISION

    assert f"VALUES (TRUE, {revision})" in schema
    assert f"WHERE singleton = TRUE) <> {revision} THEN" in schema
    assert f"does not match bootstrap revision {revision}" in schema
    assert f"complete Librae schema revision {revision}" in schema


def test_the_instrument_type_domain_survives_a_rerun() -> None:
    """CREATE DOMAIN has no IF NOT EXISTS; unwrapped, a re-run against an
    initialized database aborts under ON_ERROR_STOP and skips everything after."""
    schema = BOOTSTRAP.read_text(encoding="utf-8")
    start = schema.index("CREATE DOMAIN instrument_type_t")
    block = schema[schema.rindex("DO $$", 0, start) : schema.index("END $$;", start)]

    assert "EXCEPTION WHEN duplicate_object" in block
    assert start < schema.index("CREATE TABLE IF NOT EXISTS run_bars")


def test_gate_queries_follow_search_path() -> None:
    """A hard-coded schema prefix would break engine tables living outside public."""
    cursor = FakeCursor(CURRENT_SCHEMA_REVISION)
    require_current_schema(cursor)
    assert cursor.queries
    assert not any("public." in q for q in cursor.queries)
    assert "to_regclass('public." not in BOOTSTRAP.read_text()


def test_gate_ignores_a_revision_table_outside_the_current_schema() -> None:
    """The bootstrap creates its tables in the current schema; a current
    revision further down search_path must not vouch for that empty schema."""
    cursor = FakeCursor(
        None, search_path=("engine", "public"), copies={"public": CURRENT_SCHEMA_REVISION}
    )
    assert "has no librae_schema_revision table" in _refusal(cursor)


def test_gate_reads_the_current_schema_copy_ahead_of_a_stale_one() -> None:
    cursor = FakeCursor(
        None,
        search_path=("engine", "public"),
        copies={"engine": CURRENT_SCHEMA_REVISION, "public": CURRENT_SCHEMA_REVISION - 1},
    )
    require_current_schema(cursor)


def test_bootstrap_checks_tables_only_in_the_current_schema() -> None:
    """The unqualified CREATEs write to current_schema(); an existence check
    that searched all of search_path would see other schemas' tables instead."""
    schema = BOOTSTRAP.read_text(encoding="utf-8")
    probes = re.findall(r"to_regclass\(([^)]*)\)", schema)

    assert probes
    assert all(probe.startswith("target || '.") for probe in probes), probes
    for block in re.findall(r"DO \$\$.*?END\s*\$\$;", schema, flags=re.DOTALL):
        if "to_regclass(" in block:
            assert "target text := quote_ident(current_schema());" in block
