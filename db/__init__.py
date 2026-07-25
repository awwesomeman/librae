"""Database integration layer."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager, suppress

import psycopg2
import psycopg2.pool

TIMESCALE_DSN = os.getenv("TIMESCALE_DSN")
if not TIMESCALE_DSN:
    raise RuntimeError(
        "TIMESCALE_DSN 未設定！載入 .env 是應用層的責任（例如 uv run --env-file、"
        "direnv，或自行呼叫 load_dotenv()）——db 模組不會自動尋找/讀取 .env 檔案。"
    )

_pool = None


def get_pool(
    dsn: str = TIMESCALE_DSN, minconn: int = 1, maxconn: int = 5
) -> psycopg2.pool.SimpleConnectionPool:
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
        with suppress(Exception):
            conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=bool(conn.closed))
