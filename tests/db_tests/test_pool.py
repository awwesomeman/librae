"""Tests for db/__init__.py — connection pool lifecycle.

No existing test exercised get_conn/get_pool directly (every consumer test
mocks get_conn away), which is how a real bug shipped: SimpleConnectionPool
doesn't validate connections on checkout, so a connection killed by a
network blip kept getting handed out broken and failing every DB write
forever until the process restarted. These tests cover the pool/connection
lifecycle itself, not any higher-level DB function.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import db
import pytest


@pytest.fixture(autouse=True)
def reset_pool():
    """Each test starts with no cached pool."""
    db._pool = None
    yield
    db._pool = None


def _mock_pool(conn: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.closed = False
    pool.getconn.return_value = conn
    return pool


class TestGetConn:
    def test_healthy_connection_committed_and_returned_to_pool(self):
        conn = MagicMock()
        conn.closed = 0
        pool = _mock_pool(conn)
        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool):
            with db.get_conn("dsn") as c:
                assert c is conn
            conn.commit.assert_called_once()
            pool.putconn.assert_called_once_with(conn, close=False)

    def test_exception_rolls_back_and_still_returns_healthy_connection(self):
        conn = MagicMock()
        conn.closed = 0
        pool = _mock_pool(conn)
        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool):
            with pytest.raises(ValueError), db.get_conn("dsn"):
                raise ValueError("boom")
            conn.rollback.assert_called_once()
            pool.putconn.assert_called_once_with(conn, close=False)

    def test_dead_connection_is_discarded_not_returned(self):
        """Regression: a connection killed mid-use (server/network dropped
        it) must be discarded (close=True), not handed back to the pool
        for the next caller to fail on too."""
        conn = MagicMock()
        conn.closed = 0

        def _die(*a, **k):
            conn.closed = 2
            raise Exception("connection already closed")

        conn.commit.side_effect = _die
        pool = _mock_pool(conn)
        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool):
            with (
                pytest.raises(Exception, match="connection already closed"),
                db.get_conn("dsn"),
            ):
                pass
            pool.putconn.assert_called_once_with(conn, close=True)

    def test_rollback_failure_on_dead_connection_does_not_mask_original_error(self):
        conn = MagicMock()
        conn.closed = 2  # already dead when yielded to the caller
        conn.rollback.side_effect = Exception("rollback also fails on dead conn")
        pool = _mock_pool(conn)
        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool):
            with pytest.raises(ValueError, match="original error"), db.get_conn("dsn"):
                raise ValueError("original error")
            pool.putconn.assert_called_once_with(conn, close=True)


class TestGetPool:
    def test_missing_default_dsn_raises_when_pool_is_requested(self, monkeypatch):
        monkeypatch.delenv("TIMESCALE_DSN", raising=False)

        with pytest.raises(RuntimeError, match="TIMESCALE_DSN"):
            db.get_pool()

    def test_default_dsn_is_resolved_when_pool_is_requested(self, monkeypatch):
        monkeypatch.setenv("TIMESCALE_DSN", "postgresql://test")
        pool = MagicMock()
        pool.closed = False

        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool) as ctor:
            assert db.get_pool() is pool

        ctor.assert_called_once_with(1, 5, "postgresql://test")

    def test_reuses_existing_open_pool(self):
        pool = MagicMock()
        pool.closed = False
        with patch("psycopg2.pool.SimpleConnectionPool", return_value=pool) as ctor:
            p1 = db.get_pool("dsn")
            p2 = db.get_pool("dsn")
            assert p1 is p2
            ctor.assert_called_once()

    def test_recreates_pool_when_previous_one_is_closed(self):
        pool1 = MagicMock()
        pool1.closed = True
        pool2 = MagicMock()
        pool2.closed = False
        with patch("psycopg2.pool.SimpleConnectionPool", side_effect=[pool1, pool2]):
            p1 = db.get_pool("dsn")
            assert p1 is pool1
            p2 = db.get_pool("dsn")
            assert p2 is pool2
