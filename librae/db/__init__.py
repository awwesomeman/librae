"""Database integration layer."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager, suppress

import psycopg2
import psycopg2.pool

_pool = None


def _resolve_dsn(dsn: str | None) -> str:
    resolved = dsn if dsn is not None else os.getenv("TIMESCALE_DSN")
    if not resolved:
        raise RuntimeError(
            "TIMESCALE_DSN 未設定！載入 .env 是應用層的責任（例如 uv run --env-file、"
            "direnv，或自行呼叫 load_dotenv()）——db 模組不會自動尋找/讀取 .env 檔案。"
        )
    return resolved


@contextmanager
def admin_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a one-shot connection for schema inspection and migration.

    Separate from TIMESCALE_DSN on purpose: the application role is granted
    DML only, so it cannot ALTER a table it does not own or write
    librae_schema_revision. Falling back to TIMESCALE_DSN would either fail
    confusingly or, on a deployment that pointed it at the owner, quietly
    hand the engine schema rights.

    Deliberately not pooled. get_pool caches one pool for the process and
    ignores the DSN once it exists, so borrowing from it could hand back an
    application connection under an admin-looking call.
    """
    dsn = os.getenv("TIMESCALE_ADMIN_DSN")
    if not dsn:
        raise RuntimeError(
            "TIMESCALE_ADMIN_DSN is not set. Schema commands connect as the role that "
            "owns the tables, which TIMESCALE_DSN deliberately is not: the application "
            "role holds no schema rights. Set TIMESCALE_ADMIN_DSN to an owner "
            "connection — see docs/guides/optional-infrastructure.md."
        )
    conn = psycopg2.connect(dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        with suppress(Exception):
            conn.rollback()
        raise
    finally:
        conn.close()


def get_pool(
    dsn: str | None = None, minconn: int = 1, maxconn: int = 5
) -> psycopg2.pool.SimpleConnectionPool:
    """Return a shared SimpleConnectionPool (lazy-init, auto-recreate)."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.SimpleConnectionPool(minconn, maxconn, _resolve_dsn(dsn))
    return _pool


@contextmanager
def get_conn(
    dsn: str | None = None,
) -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a psycopg2 connection from the pool with auto-commit/rollback.

    Discards (rather than returns) connections left dead by a network blip
    or idle timeout — SimpleConnectionPool doesn't validate connections on
    checkout, so a broken one would otherwise keep getting handed out and
    failing forever until the process restarts.
    """
    pool = get_pool(dsn)
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        with suppress(Exception):
            conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=bool(conn.closed))
