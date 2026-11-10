"""Read-only database access for the console."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import psycopg2
import streamlit as st

DSN_ENV = "LIBRAE_CONSOLE_DSN"
# Short enough that a refreshed status panel reads new rows, long enough that
# several open tabs share one query instead of each running their own.
CACHE_SECONDS = 10


# A runaway query is cancelled rather than left to load a small host.
STATEMENT_TIMEOUT = "30s"


@st.cache_resource(show_spinner=False)
def _connection(dsn: str) -> psycopg2.extensions.connection:
    conn = psycopg2.connect(dsn)
    # The role should already be read-only; this makes the session refuse
    # writes too, so a misconfigured DSN still cannot change anything.
    conn.set_session(readonly=True, autocommit=True)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %s", (STATEMENT_TIMEOUT,))
    return conn


def _discard(conn: psycopg2.extensions.connection) -> None:
    conn.close()
    _connection.clear()


def _open_connection() -> psycopg2.extensions.connection:
    dsn = os.environ[DSN_ENV]  # the entry point stops before any page runs without it
    conn = _connection(dsn)
    if conn.closed:
        _discard(conn)
        conn = _connection(dsn)
    return conn


def _fetch(conn: psycopg2.extensions.connection, sql: str, params: tuple[Any, ...]) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        columns = [column.name for column in cur.description]
    return pd.DataFrame(rows, columns=columns)


@st.cache_data(ttl=CACHE_SECONDS, show_spinner=False)
def query(sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    """Run one SELECT and return its rows; results are cached for ``CACHE_SECONDS``."""
    conn = _open_connection()
    try:
        return _fetch(conn, sql, params)
    except psycopg2.OperationalError:
        # Only a connection that dropped while idle is reopened, once; a
        # cancelled or failing query on a live connection surfaces as is.
        if not conn.closed:
            raise
        _discard(conn)
        return _fetch(_open_connection(), sql, params)
