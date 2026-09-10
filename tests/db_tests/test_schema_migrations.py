from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from librae.db import schema as schema_module
from librae.db.schema import (
    _CURRENT_MARKER,
    _LEGACY_MARKER,
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
        stranded: int = 0,
    ) -> None:
        self.revision = revision
        self.core_exists = core_exists
        self.legacy_compatible = legacy_compatible
        self.execution_column = execution_column
        self.stranded = stranded
        self._result: list[tuple[object, ...]] = []
        self.executed: list[str] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append(query)
        if query == "SELECT to_regclass('public.ohlcv')":
            # Spelled literally in the source, not parameterized.
            self._result = [("public.ohlcv",)]
        elif query == "SELECT to_regclass(%s)":
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
            applied = self.revision or 0
            has_legacy_marker = (
                applied >= 2 if self.execution_column is None else self.execution_column
            )
            if has_legacy_marker:
                rows.append(_LEGACY_MARKER)
            if applied >= CURRENT_SCHEMA_REVISION:
                rows.append(_CURRENT_MARKER)
            if not self.legacy_compatible:
                # Drop a fingerprint column: an unversioned schema this build
                # cannot recognize at all.
                rows = [r for r in rows if r != ("backtest_runs", "run_id")]
            self._result = rows
        elif "count(*) FROM ohlcv WHERE calendar_id IS NULL" in query:
            self._result = [(self.stranded,)]
        elif query.startswith("SELECT revision FROM librae_schema_revision"):
            self._result = [] if self.revision is None else [(self.revision,)]
        elif "CREATE TABLE IF NOT EXISTS librae_schema_revision" in query:
            self.revision = 1
            self._result = []
        elif "ADD COLUMN execution_identity JSONB" in query:
            self.revision = 2
        elif "ADD COLUMN IF NOT EXISTS primary_subscriptions" in query:
            self.revision = 3
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


def test_known_unversioned_schema_upgrades_through_every_revision() -> None:
    status = inspect_schema(FakeCursor(revision=None, legacy_compatible=True))

    assert status.state == "upgrade_required"
    # Derived from the ladder: a revision bump should not need this edited.
    assert status.pending_revisions == tuple(range(1, CURRENT_SCHEMA_REVISION + 1))


def test_unknown_unversioned_schema_fails_closed() -> None:
    cursor = FakeCursor(revision=None, legacy_compatible=False)

    with pytest.raises(RuntimeError, match="partially applied"):
        require_current_schema(cursor)


def test_supported_upgrade_and_repeated_execution() -> None:
    cursor = FakeCursor(revision=None, legacy_compatible=True)

    assert apply_migrations(cursor) == tuple(range(1, CURRENT_SCHEMA_REVISION + 1))
    assert cursor.revision == CURRENT_SCHEMA_REVISION
    assert apply_migrations(cursor) == ()


def test_a_stamped_revision_applies_only_what_follows_it() -> None:
    cursor = FakeCursor(revision=1)

    assert apply_migrations(cursor) == tuple(range(2, CURRENT_SCHEMA_REVISION + 1))
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


def _current_cursor(stranded: int) -> FakeCursor:
    return FakeCursor(revision=CURRENT_SCHEMA_REVISION, stranded=stranded)


def _connection(cursor: FakeCursor):
    @contextmanager
    def connect(*_args: object):
        conn = MagicMock()
        conn.cursor.return_value = cursor
        yield conn

    return connect


class TestOnlyTheCliGatesStrandedRows:
    """Only a database adopted via 0003 before its backfill has stranded rows,
    and a later revision enforces NOT NULL; the engine gate stays structural."""

    def test_migrations_still_apply_while_rows_are_stranded(self) -> None:
        # apply_migrations verifies inside its own transaction, and 0003 always
        # leaves stranded rows: a data check there would roll every adoption back.
        cursor = FakeCursor(revision=None, legacy_compatible=True, stranded=42)

        assert apply_migrations(cursor) == tuple(range(1, CURRENT_SCHEMA_REVISION + 1))

    def test_the_engine_gate_ignores_stranded_rows_and_never_reads_ohlcv(self) -> None:
        cursor = _current_cursor(stranded=42)

        require_current_schema(cursor)

        assert not [query for query in cursor.executed if "FROM ohlcv" in query]

    @pytest.mark.parametrize("command", ["migrate", "preflight"])
    @pytest.mark.parametrize(("stranded", "exit_code"), [(42, 1), (0, 0)])
    def test_the_cli_exits_non_zero_while_rows_are_stranded(
        self, monkeypatch: pytest.MonkeyPatch, command: str, stranded: int, exit_code: int
    ) -> None:
        monkeypatch.setenv("TIMESCALE_DSN", "postgresql://quant_app@localhost:5432/quant")
        with (
            patch("librae.db.admin_conn", _connection(_current_cursor(stranded))),
            patch("librae.db.get_conn", _connection(_current_cursor(0))),
        ):
            assert schema_module._run_cli(command) == exit_code
