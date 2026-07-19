"""Database integration layer."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.pool

TIMESCALE_DSN = os.getenv(
    "TIMESCALE_DSN",
    "postgresql://quant:quant_secret@localhost:5432/quant",
)

_pool = None


def get_pool(dsn: str = TIMESCALE_DSN, minconn: int = 1, maxconn: int = 5) -> psycopg2.pool.SimpleConnectionPool:
    """Return a shared SimpleConnectionPool (lazy-init, auto-recreate)."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.SimpleConnectionPool(minconn, maxconn, dsn)
    return _pool


@contextmanager
def get_conn(dsn: str = TIMESCALE_DSN) -> Generator[psycopg2.extensions.connection, None, None]:
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
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        pool.putconn(conn, close=bool(conn.closed))
