"""Small, SQL-first schema revision contract for the reference database."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, Protocol

_MIGRATION_FILES = {
    1: "0001_adopt_legacy_schema.sql",
    2: "0002_bind_execution_identity.sql",
}
CURRENT_SCHEMA_REVISION = max(_MIGRATION_FILES)
SchemaState = Literal[
    "empty",
    "upgrade_required",
    "current",
    "newer",
    "unsupported_old",
    "unknown",
]

# Revision 0 is the last unversioned schema shipped before the migration
# contract.  These objects/columns are the durable execution boundary that
# must be recognizable before it is safe to adopt an existing database.
_LEGACY_REQUIRED_COLUMNS = {
    "backtest_runs": {"run_id", "config_hash", "primary_subscriptions"},
    "execution_runtime_state": {"state_key", "run_id", "config_hash", "state"},
    "broker_orders": {"state_key", "client_order_id", "run_id", "request"},
    "position_events": {"event_id", "run_id", "account_id"},
    "runtime_events": {"event_id", "run_id", "event_type"},
}


class _Cursor(Protocol):
    def execute(self, query: str, params: object = None) -> None: ...

    def fetchone(self) -> object: ...

    def fetchall(self) -> list[object]: ...


@dataclass(frozen=True)
class SchemaStatus:
    """Observed database state and the action required to use this build."""

    revision: int | None
    state: SchemaState
    pending_revisions: tuple[int, ...] = ()

    @property
    def current(self) -> bool:
        return self.state == "current"


def _relation_exists(cur: _Cursor, relation: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{relation}",))
    row = cur.fetchone()
    return bool(row and row[0] is not None)


def _schema_columns(cur: _Cursor) -> dict[str, set[str]]:
    cur.execute(
        """SELECT table_name, column_name
             FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ANY(%s)""",
        (list(_LEGACY_REQUIRED_COLUMNS),),
    )
    observed: dict[str, set[str]] = {}
    for table_name, column_name in cur.fetchall():
        observed.setdefault(str(table_name), set()).add(str(column_name))
    return observed


def _required_core_columns_are_present(observed: dict[str, set[str]]) -> bool:
    return all(
        required <= observed.get(table_name, set())
        for table_name, required in _LEGACY_REQUIRED_COLUMNS.items()
    )


def _legacy_schema_is_compatible(observed: dict[str, set[str]]) -> bool:
    return _required_core_columns_are_present(
        observed
    ) and "execution_identity" not in observed.get("backtest_runs", set())


def _current_schema_is_compatible(observed: dict[str, set[str]]) -> bool:
    return _required_core_columns_are_present(observed) and "execution_identity" in observed.get(
        "backtest_runs", set()
    )


def inspect_schema(cur: _Cursor) -> SchemaStatus:
    """Inspect without changing the database or importing broker code."""
    has_revision = _relation_exists(cur, "librae_schema_revision")
    has_core_schema = _relation_exists(cur, "backtest_runs")
    if not has_revision:
        if not has_core_schema:
            return SchemaStatus(None, "empty")
        if _legacy_schema_is_compatible(_schema_columns(cur)):
            return SchemaStatus(0, "upgrade_required", tuple(range(1, CURRENT_SCHEMA_REVISION + 1)))
        return SchemaStatus(None, "unknown")

    cur.execute("SELECT revision FROM librae_schema_revision WHERE singleton = TRUE")
    rows = cur.fetchall()
    if len(rows) != 1 or isinstance(rows[0][0], bool):
        return SchemaStatus(None, "unknown")
    revision = int(rows[0][0])
    observed = _schema_columns(cur)
    if revision == CURRENT_SCHEMA_REVISION:
        return (
            SchemaStatus(revision, "current")
            if _current_schema_is_compatible(observed)
            else SchemaStatus(revision, "unknown")
        )
    if 0 <= revision < CURRENT_SCHEMA_REVISION:
        if revision in (0, 1) and not _legacy_schema_is_compatible(observed):
            return SchemaStatus(revision, "unknown")
        return SchemaStatus(
            revision,
            "upgrade_required",
            tuple(range(revision + 1, CURRENT_SCHEMA_REVISION + 1)),
        )
    if revision > CURRENT_SCHEMA_REVISION:
        return SchemaStatus(revision, "newer")
    return SchemaStatus(revision, "unsupported_old")


def _status_error(status: SchemaStatus) -> RuntimeError:
    if status.state == "empty":
        detail = "database is empty; apply librae/db/timescale_init.sql"
    elif status.state == "upgrade_required":
        detail = (
            f"schema revision {status.revision} requires migrations "
            f"{list(status.pending_revisions)}; run `librae db migrate` after a backup"
        )
    elif status.state == "newer":
        detail = (
            f"database revision {status.revision} is newer than this build's "
            f"revision {CURRENT_SCHEMA_REVISION}; deploy a compatible Librae build"
        )
    elif status.state == "unsupported_old":
        detail = f"database revision {status.revision} is no longer supported"
    else:
        detail = "database is unversioned or partially applied and does not match a known schema"
    return RuntimeError(f"incompatible Librae database schema: {detail}")


def require_current_schema(cur: _Cursor) -> None:
    """Fail closed before persistence or order-capable startup."""
    status = inspect_schema(cur)
    if not status.current:
        raise _status_error(status)


def apply_migrations(cur: _Cursor) -> tuple[int, ...]:
    """Apply supported migrations inside the caller's transaction."""
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("librae-schema",))
    status = inspect_schema(cur)
    if status.current:
        return ()
    if status.state != "upgrade_required":
        raise _status_error(status)

    applied: list[int] = []
    for revision in status.pending_revisions:
        resource = files("librae.db").joinpath("migrations", _MIGRATION_FILES[revision])
        cur.execute(resource.read_text(encoding="utf-8"))
        applied.append(revision)
    require_current_schema(cur)
    return tuple(applied)


def _run_cli(command: str) -> int:
    from librae.db import get_conn

    with get_conn() as conn:
        cur = conn.cursor()
        if command == "migrate":
            applied = apply_migrations(cur)
            print(
                f"schema revision {CURRENT_SCHEMA_REVISION} is current"
                + (f"; applied {list(applied)}" if applied else "; no migrations required")
            )
            return 0
        status = inspect_schema(cur)
        print(
            f"schema state={status.state} revision={status.revision} "
            f"pending={list(status.pending_revisions)}"
        )
        return 0 if status.current else 1
