"""A database that stops answering fails each live write within a bound.

Without one, libpq waits on such a server for as long as the OS retries, and
the live loop waits with it: halts, cancels and the order lookup after a
failed placement. The server here accepts connections and never answers, so
the connect bound is what ends each call; an established connection losing
its network is the kernel's to detect, which no offline test can stage.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import suppress

import librae.db as db
import psycopg2
import pytest
from librae.db.timescale_state import TimescaleLiveStateStore
from librae.live.state import StateStoreUnavailableError

from tests.engine.test_halt_runtime_events import _buy_btc, _live_trader
from tests.engine.test_live_flatten import _adapter, _titled, _trader
from tests.engine.test_live_runner import _mock_order_adapter

# Lowered from the default so the suite stays fast; the bound is what matters.
CONNECT_TIMEOUT_S = 1
# One attempt per write: the store reports the timeout as unavailable, so the
# engine does not retry it with the snapshot alone to look for a refused row.
WRITE_BOUND_S = CONNECT_TIMEOUT_S + 1.5
# A client with no bound then fails these tests instead of hanging the suite.
SERVER_GIVES_UP_S = 15


@pytest.fixture
def unanswering_dsn(monkeypatch) -> Iterator[str]:
    """A DSN for a server that accepts connections and never answers."""
    monkeypatch.setitem(db._CONNECT_PARAMS, "connect_timeout", str(CONNECT_TIMEOUT_S))
    monkeypatch.setattr(db, "_pool", None)
    server = socket.create_server(("127.0.0.1", 0))
    server.settimeout(0.1)
    stop = threading.Event()

    def hold_connections() -> None:
        held: list[tuple[socket.socket, float]] = []
        while not stop.is_set():
            with suppress(TimeoutError):
                held.append((server.accept()[0], time.monotonic()))
            for conn, accepted_at in list(held):
                if time.monotonic() - accepted_at > SERVER_GIVES_UP_S:
                    conn.close()
                    held.remove((conn, accepted_at))
        for conn, _ in held:
            conn.close()

    thread = threading.Thread(target=hold_connections, daemon=True)
    thread.start()
    yield f"host=127.0.0.1 port={server.getsockname()[1]} dbname=librae user=librae"
    stop.set()
    thread.join()
    server.close()


def _store_on(trader, dsn: str) -> dict[str, bool]:
    """Send checkpoints to the database at ``dsn`` while ``outage['down']`` is set."""
    memory_save = trader._state_store.save
    database = TimescaleLiveStateStore(dsn)
    outage = {"down": True}

    def save(state, orders=()):
        (database.save if outage["down"] else memory_save)(state, orders)

    trader._state_store.save = save
    return outage


def test_a_connection_to_it_fails_within_the_bound(unanswering_dsn):
    started = time.monotonic()

    with (
        pytest.raises(psycopg2.OperationalError, match="timeout expired"),
        db.get_conn(unanswering_dsn),
    ):
        pass

    assert time.monotonic() - started < WRITE_BOUND_S


def test_a_halt_alerts_within_the_bound(unanswering_dsn):
    trader, _, alerts = _trader(_adapter())
    _store_on(trader, unanswering_dsn)
    started = time.monotonic()

    with pytest.raises(StateStoreUnavailableError, match="timeout expired"):
        trader.halt("desk asked to stop")

    assert time.monotonic() - started < WRITE_BOUND_S
    assert trader._halted is True
    [halt] = _titled(alerts, "Manual Halt")
    assert "halt not persisted (StateStoreUnavailableError" in halt["message"]


def test_a_failed_placement_reaches_its_lookup_within_the_bound(unanswering_dsn):
    adapter = _mock_order_adapter()
    adapter.find_order.return_value = None
    trader, _, alerts = _live_trader(adapter, "BTCUSDT")
    outage = _store_on(trader, unanswering_dsn)
    outage["down"] = False
    times: dict[str, float] = {}

    def place_order(*_args):
        outage["down"] = True
        times["placed"] = time.monotonic()
        raise TimeoutError("read timed out")

    def find_order(*_args):
        times["looked_up"] = time.monotonic()

    adapter.place_order.side_effect = place_order
    adapter.find_order.side_effect = find_order

    with pytest.raises(StateStoreUnavailableError, match="timeout expired"):
        _buy_btc(trader)

    assert times["looked_up"] - times["placed"] < WRITE_BOUND_S
    assert time.monotonic() - times["looked_up"] < WRITE_BOUND_S
    assert trader._halted is True
    [halt] = _titled(alerts, "Ambiguous Order Placement")
    assert "halt not persisted (StateStoreUnavailableError" in halt["message"]
