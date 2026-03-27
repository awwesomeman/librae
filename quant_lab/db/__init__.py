"""Database integration layer."""
from __future__ import annotations

import os

import psycopg2.pool

TIMESCALE_DSN = os.getenv(
    "TIMESCALE_DSN",
    "postgresql://quant:quant_secret@localhost:5432/quant",
)

_pool = None


def get_pool(dsn: str = TIMESCALE_DSN, minconn: int = 1, maxconn: int = 5):
    """Return a shared SimpleConnectionPool (lazy-init, auto-recreate)."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.SimpleConnectionPool(minconn, maxconn, dsn)
    return _pool
