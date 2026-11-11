"""Database integration layer."""

from __future__ import annotations

import os
from collections.abc import Generator, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar

import psycopg2
import psycopg2.extensions
import psycopg2.pool

_pool = None

# Without these, a call to a server that stopped answering waits as long as
# the OS keeps retrying (tcp(7): tcp_syn_retries, tcp_retries2,
# tcp_keepalive_time), minutes to hours. None fires while the server answers,
# however slowly.
_CONNECT_PARAMS = {"connect_timeout": "5"}
# tcp_user_timeout ends a call whose sent data the server never acknowledges,
# and keepalive probes end one whose reply was lost after the server took the
# query. libpq ignores tcp_user_timeout where the OS lacks TCP_USER_TIMEOUT,
# macOS among them; there unacknowledged data waits out the OS
# retransmission limit.
_SILENCE_PARAMS = {
    "tcp_user_timeout": "10000",
    "keepalives_idle": "5",
    "keepalives_interval": "5",
    "keepalives_count": "2",
}

# For a caller that must not wait on the database. The statement timeout is
# server-side, so it covers a server that answers TCP but whose statement
# hangs, e.g. on a lock. The idle timeout frees the locks of a transaction
# whose client gave up mid-way, e.g. on a lost COMMIT, which the server would
# otherwise hold until its own keepalive noticed (two hours by default); a
# bounded transaction never idles between statements, and the same 10 s
# frees the locks about when the client gives up. Not set for every caller:
# a backtest or bulk load may legitimately run long statements.
_BOUND_TRANSACTION = (
    "SET LOCAL statement_timeout = 10000; SET LOCAL idle_in_transaction_session_timeout = 10000"
)
_bounded: ContextVar[bool] = ContextVar("bounded", default=False)


def _storable_text(text: str) -> str:
    """Return ``text`` as PostgreSQL stores it: NUL dropped, lone surrogates escaped.

    psycopg2 cannot encode a lone surrogate, and PostgreSQL refuses a NUL in
    text and either one as a jsonb escape, so a single such character in a
    venue's error text would fail the whole write. Neither change can be
    undone on read: an escaped surrogate reads back as the six characters
    ``\\udc80``, the same as text that held them literally, and a dropped
    NUL is gone.
    """
    return text.replace("\x00", "").encode("utf-8", "backslashreplace").decode("utf-8")


def _resolve_dsn(dsn: str | None) -> str:
    resolved = dsn if dsn is not None else os.getenv("TIMESCALE_DSN")
    if not resolved:
        raise RuntimeError(
            "TIMESCALE_DSN 未設定！載入 .env 是應用層的責任（例如 uv run --env-file、"
            "direnv，或自行呼叫 load_dotenv()）——db 模組不會自動尋找/讀取 .env 檔案。"
        )
    return resolved


def _params_unset_by(dsn: str, params: dict[str, str]) -> dict[str, str]:
    """``params`` minus those ``dsn`` sets, so a deployment can override each one."""
    in_dsn = psycopg2.extensions.parse_dsn(dsn)
    return {key: value for key, value in params.items() if key not in in_dsn}


def get_pool(
    dsn: str | None = None, minconn: int = 1, maxconn: int = 5
) -> psycopg2.pool.SimpleConnectionPool:
    """Return a shared SimpleConnectionPool (lazy-init, auto-recreate)."""
    global _pool
    if _pool is None or _pool.closed:
        resolved = _resolve_dsn(dsn)
        params = _params_unset_by(resolved, {**_CONNECT_PARAMS, **_SILENCE_PARAMS})
        _pool = psycopg2.pool.SimpleConnectionPool(minconn, maxconn, resolved, **params)
    return _pool


def _connect_unpooled(dsn: str | None = None) -> psycopg2.extensions.connection:
    """Open a connection for a session held idle for the process lifetime.

    Only its connect is bounded; it keeps the OS TCP timing otherwise. Probed
    or timed out like a pooled connection, a short outage would end it on
    this side only; the server would hold the session, and its advisory
    locks, until its own keepalive gave up, then free them while this process
    still counts itself their owner. Ending the session at shutdown on a dead
    network therefore waits out the OS retransmission limit.
    """
    resolved = _resolve_dsn(dsn)
    return psycopg2.connect(resolved, **_params_unset_by(resolved, _CONNECT_PARAMS))


@contextmanager
def _bounded_statements() -> Iterator[None]:
    """Bound each transaction ``get_conn`` opens in this block (``_BOUND_TRANSACTION``).

    Set per transaction, so no pooled connection carries it to another
    caller. A timed-out statement raises ``QueryCanceled``.
    """
    token = _bounded.set(True)
    try:
        yield
    finally:
        _bounded.reset(token)


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
        if _bounded.get():
            with conn.cursor() as cur:
                cur.execute(_BOUND_TRANSACTION)
        yield conn
        conn.commit()
    except Exception:
        with suppress(Exception):
            conn.rollback()
        raise
    finally:
        pool.putconn(conn, close=bool(conn.closed))
