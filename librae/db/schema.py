"""Fail-closed schema revision gate for the reference database.

Librae owns the current schema: ``timescale_init.sql`` bootstraps an empty
database and stamps its revision. Upgrading an existing database is the
deployment's job; the spec is the ``timescale_init.sql`` diff between versions.
"""

from __future__ import annotations

from typing import Protocol

CURRENT_SCHEMA_REVISION = 3


class _Cursor(Protocol):
    def execute(self, query: str, params: object = None) -> None: ...

    def fetchone(self) -> object: ...


def require_current_schema(cur: _Cursor) -> None:
    """Fail closed before persistence or order-capable startup."""
    # to_regclass, not a bare SELECT: a missing table would abort the caller's transaction.
    cur.execute("SELECT to_regclass('public.librae_schema_revision')")
    row = cur.fetchone()
    if row is None or row[0] is None:
        found = "has no librae_schema_revision table"
    else:
        cur.execute("SELECT revision FROM librae_schema_revision WHERE singleton = TRUE")
        row = cur.fetchone()
        if row is None:
            found = "has no revision row"
        elif row[0] == CURRENT_SCHEMA_REVISION:
            return
        else:
            found = f"is at revision {row[0]}"
    raise RuntimeError(
        f"incompatible Librae database schema: the database {found}, this build requires "
        f"revision {CURRENT_SCHEMA_REVISION}. Bootstrap an empty database with "
        "librae/db/timescale_init.sql; an existing database must be brought to revision "
        f"{CURRENT_SCHEMA_REVISION} by its deployment, matching timescale_init.sql of this build."
    )
