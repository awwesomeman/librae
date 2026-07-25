"""Database integration layer."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager, suppress

import psycopg2
import psycopg2.pool


def _load_dotenv() -> None:
    """Load .env from project root if present, without overwriting existing env vars."""
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env_path = os.path.join(root_dir, ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip()
            if not val.startswith(("'", '"')):
                val = val.split(" #", 1)[0].rstrip()  # strip trailing ` # comment`
            val = val.strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


_load_dotenv()

TIMESCALE_DSN = os.getenv("TIMESCALE_DSN")
if not TIMESCALE_DSN:
    raise RuntimeError(
        "TIMESCALE_DSN 未設定！請確認環境變數或根目錄下的 .env 檔案中是否包含 TIMESCALE_DSN。"
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
