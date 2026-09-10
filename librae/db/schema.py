"""Small, SQL-first schema revision contract for the reference database."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Literal, Protocol

_MIGRATION_FILES = {
    1: "0001_adopt_legacy_schema.sql",
    2: "0002_bind_execution_identity.sql",
    3: "0003_adopt_subscription_identity.sql",
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
#
# Identity only: a column a later release added is not part of recognizing a
# database as Librae's.  Requiring one here refused a real production
# database that predated it, leaving it unmigratable by the very tool meant
# to adopt it.  Feature columns belong in _REVISION_MARKERS.
_LEGACY_REQUIRED_COLUMNS = {
    "backtest_runs": {"run_id", "config_hash"},
    "execution_runtime_state": {"state_key", "run_id", "config_hash", "state"},
    "broker_orders": {"state_key", "client_order_id", "run_id", "request"},
    "position_events": {"event_id", "run_id", "account_id"},
    "runtime_events": {"ts", "run_id", "event_type"},
}

# The column the newest migration adds, and the one the first adds.  "current"
# looks for the newer rather than trusting a stamped number.
_CURRENT_MARKER = ("ohlcv", "available_at")
_LEGACY_MARKER = ("backtest_runs", "execution_identity")

_INSPECTED_TABLES = sorted({*_LEGACY_REQUIRED_COLUMNS, _CURRENT_MARKER[0]})


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
        (_INSPECTED_TABLES,),
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


def _has(observed: dict[str, set[str]], marker: tuple[str, str]) -> bool:
    table, column = marker
    return column in observed.get(table, set())


def _legacy_schema_is_compatible(observed: dict[str, set[str]]) -> bool:
    return _required_core_columns_are_present(observed) and not _has(observed, _LEGACY_MARKER)


def _current_schema_is_compatible(observed: dict[str, set[str]]) -> bool:
    return _required_core_columns_are_present(observed) and _has(observed, _CURRENT_MARKER)


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
        pending = tuple(range(revision + 1, CURRENT_SCHEMA_REVISION + 1))
        # A revision whose forward migration has since been retired cannot be
        # upgraded by this build. Report that up front instead of letting
        # apply_migrations() fail on a missing file partway through the run.
        if not all(step in _MIGRATION_FILES for step in pending):
            return SchemaStatus(revision, "unsupported_old")
        return SchemaStatus(revision, "upgrade_required", pending)
    if revision > CURRENT_SCHEMA_REVISION:
        return SchemaStatus(revision, "newer")
    return SchemaStatus(revision, "unknown")


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
        detail = (
            f"database revision {status.revision} is no longer upgradable by this build; "
            "migrate it with the last Librae build that still shipped its migration"
        )
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


_STRANDED_MESSAGE = (
    "WARNING: {count} ohlcv row(s) have a null calendar_id. Reads filter on that "
    "column, so those rows are invisible until the column is filled in. The "
    "schema-adoption steps are in the Optional infrastructure guide: "
    "https://github.com/awwesomeman/librae/blob/main/docs/guides/"
    "optional-infrastructure.md"
)


def _unbackfilled_ohlcv_rows(cur: _Cursor) -> int:
    """Rows the reader cannot see because their session identity is null.

    load_ohlcv filters on calendar_id, so a row whose value the expand
    migration left null is invisible rather than merely incomplete. Reported
    by preflight because the gap is silent everywhere else: the writer keeps
    working, the schema reads as current, and only a query comes back short.
    """
    cur.execute("SELECT to_regclass('public.ohlcv')")
    row = cur.fetchone()
    if not (row and row[0] is not None):
        return 0
    cur.execute("SELECT count(*) FROM ohlcv WHERE calendar_id IS NULL")
    result = cur.fetchone()
    return int(result[0]) if result else 0


def _describe(role: str, status: SchemaStatus) -> None:
    print(
        f"[{role}] schema state={status.state} revision={status.revision} "
        f"pending={list(status.pending_revisions)}"
    )


def _run_cli(command: str) -> int:
    import os

    from librae.db import admin_conn, get_conn

    # Migration needs the role that owns the tables, so both actions start on
    # the admin connection.
    with admin_conn() as conn:
        cur = conn.cursor()
        if command == "migrate":
            applied = apply_migrations(cur)
            print(
                f"schema revision {CURRENT_SCHEMA_REVISION} is current"
                + (f"; applied {list(applied)}" if applied else "; no migrations required")
            )
            # Said here rather than left to a later preflight: the operator's
            # next step is to start the engine, and this gap does not announce
            # itself — reads simply come back short.
            stranded = _unbackfilled_ohlcv_rows(cur)
            if stranded:
                print(
                    "The schema migration succeeded and is not rolled back; rerunning "
                    "migrate will not help. Next step: the ohlcv backfill in the adoption guide."
                )
                print(_STRANDED_MESSAGE.format(count=stranded))
                # Non-zero: the schema moved, but the data it describes is not
                # yet readable, and the operator's next step is to start the
                # engine. A green exit here would say the adoption finished.
                return 1
            return 0
        admin_status = inspect_schema(cur)
        _describe("admin", admin_status)
        stranded = _unbackfilled_ohlcv_rows(cur)
        if stranded:
            print(_STRANDED_MESSAGE.format(count=stranded))

    # The engine reads the schema as the application role, and
    # information_schema.columns is permission-filtered: a table the owner can
    # see every column of shows none to a role that was never granted it. An
    # owner-only pass would then report "current" while the engine's own
    # require_current_schema fails closed on the same database.
    application_dsn = os.getenv("TIMESCALE_DSN")
    if not application_dsn:
        print("TIMESCALE_DSN is not set; the application role's view was not checked")
        return 0 if admin_status.current and not stranded else 1
    with get_conn(application_dsn) as conn:
        application_status = inspect_schema(conn.cursor())
    _describe("application", application_status)
    return 0 if admin_status.current and application_status.current and not stranded else 1
