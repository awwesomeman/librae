from __future__ import annotations

from pathlib import Path

import pytest
from librae.db import schema as schema_module
from librae.db.schema import (
    _LEGACY_REQUIRED_COLUMNS,
    CURRENT_SCHEMA_REVISION,
    apply_migrations,
    inspect_schema,
    require_current_schema,
)


class FakeCursor:
    def __init__(
        self,
        *,
        revision: int | None = None,
        core_exists: bool = True,
        legacy_compatible: bool = True,
        execution_column: bool | None = None,
    ) -> None:
        self.revision = revision
        self.core_exists = core_exists
        self.legacy_compatible = legacy_compatible
        self.execution_column = execution_column
        self._result: list[tuple[object, ...]] = []
        self.executed: list[str] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append(query)
        if query == "SELECT to_regclass(%s)":
            relation = params[0]
            exists = (
                self.revision is not None if "schema_revision" in relation else self.core_exists
            )
            self._result = [(relation if exists else None,)]
        elif "information_schema.columns" in query:
            # Derived, not restated: a fixture with its own column list is how
            # a required column that no table has passed review — the guard and
            # the fixture agreed with each other and neither with the schema.
            rows = [
                (table, column)
                for table, columns in _LEGACY_REQUIRED_COLUMNS.items()
                for column in sorted(columns)
            ]
            has_execution_column = (
                self.revision is not None and self.revision >= 2
                if self.execution_column is None
                else self.execution_column
            )
            if has_execution_column:
                rows.append(("backtest_runs", "execution_identity"))
            self._result = rows if self.legacy_compatible else rows[:-1]
        elif query.startswith("SELECT revision FROM librae_schema_revision"):
            self._result = [] if self.revision is None else [(self.revision,)]
        elif "CREATE TABLE IF NOT EXISTS librae_schema_revision" in query:
            self.revision = 1
            self._result = []
        elif "ADD COLUMN execution_identity JSONB" in query:
            self.revision = 2
            self._result = []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


def test_empty_database_requires_bootstrap() -> None:
    status = inspect_schema(FakeCursor(revision=None, core_exists=False))

    assert status.state == "empty"
    assert status.revision is None


def test_known_unversioned_schema_has_one_supported_upgrade() -> None:
    status = inspect_schema(FakeCursor(revision=None, legacy_compatible=True))

    assert status.state == "upgrade_required"
    assert status.pending_revisions == (1, 2)


def test_unknown_unversioned_schema_fails_closed() -> None:
    cursor = FakeCursor(revision=None, legacy_compatible=False)

    with pytest.raises(RuntimeError, match="partially applied"):
        require_current_schema(cursor)


def test_supported_upgrade_and_repeated_execution() -> None:
    cursor = FakeCursor(revision=None, legacy_compatible=True)

    assert apply_migrations(cursor) == (1, 2)
    assert cursor.revision == CURRENT_SCHEMA_REVISION
    assert apply_migrations(cursor) == ()


def test_revision_one_applies_only_revision_two() -> None:
    cursor = FakeCursor(revision=1)

    assert apply_migrations(cursor) == (2,)
    assert cursor.revision == CURRENT_SCHEMA_REVISION


def test_partially_applied_revision_two_is_rejected() -> None:
    cursor = FakeCursor(revision=1, execution_column=True)

    with pytest.raises(RuntimeError, match="partially applied"):
        apply_migrations(cursor)


@pytest.mark.parametrize("revision", [-1, CURRENT_SCHEMA_REVISION + 1])
def test_incompatible_revision_fails_closed(revision: int) -> None:
    cursor = FakeCursor(revision=revision)

    with pytest.raises(RuntimeError, match="incompatible Librae database schema"):
        require_current_schema(cursor)


def test_retired_migration_is_reported_before_it_is_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retiring an old migration must fail closed at inspection rather than
    dying on a missing file partway through apply_migrations()."""
    monkeypatch.setattr(
        schema_module,
        "_MIGRATION_FILES",
        {CURRENT_SCHEMA_REVISION: "0002_bind_execution_identity.sql"},
    )
    cursor = FakeCursor(revision=0)

    assert inspect_schema(cursor).state == "unsupported_old"
    with pytest.raises(RuntimeError, match="no longer upgradable"):
        apply_migrations(cursor)


def test_bootstrap_is_current_and_does_not_embed_upgrade_ddl() -> None:
    schema = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS librae_schema_revision" in schema
    assert "ALTER TABLE" not in schema
    assert "BEGIN;" in schema
    assert schema.rstrip().endswith("COMMIT;")


def test_bootstrap_states_one_revision_everywhere_it_is_written() -> None:
    """The bootstrap stamps, verifies, and names its revision in separate
    statements; a bump that misses one would refuse every fresh database."""
    schema = Path("librae/db/timescale_init.sql").read_text(encoding="utf-8")
    revision = CURRENT_SCHEMA_REVISION

    assert f"VALUES (TRUE, {revision})" in schema
    assert f"WHERE singleton = TRUE) <> {revision} THEN" in schema
    assert f"does not match bootstrap revision {revision}" in schema
    assert f"complete Librae schema revision {revision}" in schema
