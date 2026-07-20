"""IBKRAdapter — Interactive Brokers adapter for US equities.

Wraps ``ib_async`` (community-maintained continuation of the archived
``ib_insync``) to provide the same duck-typed interface as
ShioajiAdapter/CryptoAdapter: ``fetch_ohlcv``, ``place_order``, ``get_position``.

Unlike Shioaji (login+CA) or CCXT (API key), IBKR authenticates at the
TWS/IB Gateway process, not per-adapter — the adapter just opens a socket
connection to an already-running, already-logged-in gateway. Paper vs live
is which port that gateway listens on (7497/4002 = paper, 7496/4001 = live),
not something this adapter chooses.

Credentials can be passed explicitly or loaded from environment variables
using the ``IBKR_`` prefix convention::

    IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID

Install: ``pip install ib-async`` or ``pip install -e '.[us-live]'``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from .base import AdapterInfo, CredentialConfig

logger = logging.getLogger(__name__)

# ccxt-style timeframe -> IBKR barSizeSetting string.
_BAR_SIZE_MAP = {
    "1m": "1 min", "2m": "2 mins", "3m": "3 mins", "5m": "5 mins",
    "10m": "10 mins", "15m": "15 mins", "20m": "20 mins", "30m": "30 mins",
    "1h": "1 hour", "2h": "2 hours", "3h": "3 hours", "4h": "4 hours", "8h": "8 hours",
    "1d": "1 day", "1w": "1 week", "1M": "1 month",
}


def _require_ib_async():
    """Import and return ib_async, raising a friendly error if missing."""
    try:
        import ib_async
        return ib_async
    except ImportError as e:
        raise ImportError(
            "ib_async is required for IBKRAdapter. "
            "Install it with: pip install ib-async  "
            "or: pip install -e '.[us-live]'"
        ) from e


def _to_bar_size(timeframe: str) -> str:
    if timeframe not in _BAR_SIZE_MAP:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r} for IBKRAdapter. "
            f"Supported: {sorted(_BAR_SIZE_MAP)}"
        )
    return _BAR_SIZE_MAP[timeframe]


def _default_duration_str(timeframe: str, limit: int) -> str:
    """Approximate durationStr covering `limit` bars when no start/end is
    given — IBKR wants a duration, not a bar count, for reqHistoricalData."""
    from librae.core.utils import interval_to_timedelta

    total_seconds = interval_to_timedelta(timeframe).total_seconds() * limit
    days = max(1, int(-(-total_seconds // 86400)))  # ceil division
    return f"{days} D"


@dataclass
class IBKRCredentials(CredentialConfig):
    """Connection info for a running TWS/IB Gateway instance.

    Not API-key credentials — IBKR authenticates the human at the gateway
    login screen; this only says which socket to dial. Port convention:
    7497 TWS paper, 7496 TWS live, 4002 IB Gateway paper, 4001 IB Gateway live.
    """

    host: str = "127.0.0.1"
    port: str = "7497"
    client_id: str = "1"


class IBKRAdapter:
    """US equities adapter backed by Interactive Brokers via ib_async.

    Parameters
    ----------
    credentials : IBKRCredentials | None
        If None, loads from env vars with ``IBKR_`` prefix.
    trading_enabled : bool
        If False (default), connects with IBKR's own ``readonly`` socket
        flag — ``place_order``/``get_position`` raise ``NotImplementedError``
        without ever reaching the gateway's order-entry path. Mirrors
        ShioajiAdapter's CA-gated read-only default and CryptoAdapter's
        no-api-key read-only default: safe unless explicitly opted in.

    The adapter connects during ``__init__``. Call ``close()`` or use as a
    context manager to disconnect.
    """

    def __init__(
        self,
        credentials: IBKRCredentials | None = None,
        trading_enabled: bool = False,
    ) -> None:
        ib_async = _require_ib_async()
        creds = credentials or IBKRCredentials.from_env("IBKR")

        self._ib = ib_async.IB()
        self._read_only = not trading_enabled
        self._ib.connect(
            creds.host, int(creds.port), clientId=int(creds.client_id),
            readonly=self._read_only,
        )
        logger.info(
            "IBKR connected host=%s port=%s clientId=%s trading_enabled=%s",
            creds.host, creds.port, creds.client_id, trading_enabled,
        )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="ibkr",
            venue="IBKR",
            market_type="us_equity",
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV via IBKR's reqHistoricalData.

        Args:
            symbol: Stock ticker (e.g. ``"MU"``).
            timeframe: ccxt-format candle interval — one of
                ``_BAR_SIZE_MAP`` (e.g. ``"1m"``, ``"1h"``, ``"1d"``).
            start/end: Date range as datetime or ``"YYYY-MM-DD"`` string.
                If omitted, fetches the most recent *limit* bars.
            limit: Max bars (used only when start/end are omitted).

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a UTC-aware datetime.

        ``useRTH=False`` includes extended-hours prints — IBKR's own
        pacing/lookback limits per bar size (e.g. 1-sec bars only go back
        a few days) apply and aren't paginated around here; a window too
        long for the requested bar size raises from ib_async directly.
        """
        contract = self._resolve_contract(symbol)
        bar_size = _to_bar_size(timeframe)

        end_dt = _parse_dt(end) if end else datetime.now(timezone.utc)
        if start:
            start_dt = _parse_dt(start)
            duration = f"{max(1, (end_dt - start_dt).days + 1)} D"
        else:
            duration = _default_duration_str(timeframe, limit)

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,  # UTC datetimes, not exchange-local strings
        )

        ib_async = _require_ib_async()
        df = ib_async.util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        df = df.rename(columns={"date": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df[["ts", "open", "high", "low", "close", "volume"]]

        if start:
            df = df[(df["ts"] >= start_dt) & (df["ts"] <= end_dt)]
        elif len(df) > limit:
            df = df.tail(limit)
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Order management (requires trading_enabled=True)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "IBKRAdapter connected read-only — pass trading_enabled=True "
                "to enable order placement."
            )

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side`` (``"buy"``/``"sell"``),
        ``quantity``, ``order_type`` (``"market"``/``"limit"``),
        and optionally ``price`` for limit orders.
        """
        self._require_auth()
        ib_async = _require_ib_async()

        contract = self._resolve_contract(signal["symbol"])
        action = "BUY" if signal["side"] == "buy" else "SELL"
        if signal.get("order_type") == "limit":
            order = ib_async.LimitOrder(action, signal["quantity"], signal["price"])
        else:
            order = ib_async.MarketOrder(action, signal["quantity"])

        trade = self._ib.placeOrder(contract, order)
        self._ib.sleep(0)  # pump the event loop so orderStatus reflects the ack
        return {
            "id": str(trade.order.orderId),
            "status": trade.orderStatus.status,
        }

    def get_position(self, symbol: str) -> dict:
        """Return current position for *symbol*.

        ``unrealized_pnl`` is always 0.0 — IBKR's ``positions()`` doesn't
        carry live uPnL (that needs a separate ``reqPnLSingle`` subscription
        per symbol); not implemented here, matches the scope of the other
        adapters' position snapshot.
        """
        self._require_auth()

        for pos in self._ib.positions():
            if pos.contract.symbol == symbol:
                return {
                    "symbol": symbol,
                    "size": pos.position,
                    "avg_price": pos.avgCost,
                    "unrealized_pnl": 0.0,
                }
        return {"symbol": symbol, "size": 0, "avg_price": 0, "unrealized_pnl": 0.0}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect from TWS/IB Gateway."""
        self._ib.disconnect()
        logger.info("IBKR disconnected")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_contract(self, symbol: str):
        """Resolve a ticker string to a qualified IBKR Stock contract
        (SMART routing, USD) — qualifyContracts hits IBKR to fill in
        conId/exchange details and confirms the symbol actually exists."""
        ib_async = _require_ib_async()
        contract = ib_async.Stock(symbol, "SMART", "USD")
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"Unknown symbol: {symbol}")
        return qualified[0]


def _parse_dt(dt: datetime | str) -> datetime:
    """Parse a datetime or 'YYYY-MM-DD' string to a UTC-aware datetime."""
    if isinstance(dt, str):
        parsed = pd.Timestamp(dt)
    else:
        parsed = pd.Timestamp(dt)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC").to_pydatetime()
