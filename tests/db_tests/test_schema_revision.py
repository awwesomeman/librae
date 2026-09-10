"""The fail-closed schema revision gate and the bootstrap that stamps it."""

from __future__ import annotations

from pathlib import Path

import pytest
from librae.db.schema import CURRENT_SCHEMA_REVISION, require_current_schema

BOOTSTRAP = Path(__file__).resolve().parents[2] / "librae/db/timescale_init.sql"


class FakeCursor:
    """Answers only the reads the gate may make; quant_app is granted no more."""

    def __init__(self, revision: int | None, *, has_table: bool = True) -> None:
        self.revision = revision
        self.has_table = has_table
        self._row: tuple[object, ...] | None = None

    def execute(self, query: str, params: object = None) -> None:
        if query == "SELECT to_regclass('public.librae_schema_revision')":
            self._row = ("librae_schema_revision" if self.has_table else None,)
        elif query == "SELECT revision FROM librae_schema_revision WHERE singleton = TRUE":
            self._row = None if self.revision is None else (self.revision,)
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
