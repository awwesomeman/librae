"""CryptoAdapter — CCXT-based market adapter for crypto exchanges.

Wraps CCXT to provide a simple duck-typed interface compatible with
signal_monitor's ``fetch_ohlcv`` protocol, plus optional order/position
methods when API credentials are supplied.

Reusable across any CCXT-supported exchange (set via ``exchange_id``), so
credential loading takes an explicit prefix per exchange rather than a fixed
one — e.g. ``CryptoCredentials.from_env("BINANCE")`` reads ``BINANCE_API_KEY``,
``BINANCE_API_SECRET``, ``BINANCE_EXCHANGE_ID``, ``BINANCE_SANDBOX``. Adding a
second exchange means picking a new prefix (e.g. ``OKX_*``), not new code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from .base import AdapterInfo, CredentialConfig, find_position

logger = logging.getLogger(__name__)


def _require_ccxt() -> object:
    """Import and return ccxt, raising a friendly error if missing."""
    try:
        import ccxt

        return ccxt
    except ImportError as e:
        raise ImportError(
            "ccxt is required for CryptoAdapter. Install it with: pip install ccxt"
        ) from e


def _timeframe_to_delta(timeframe: str) -> pd.Timedelta:
    """Convert CCXT timeframe string to a pandas Timedelta."""
    from librae.core.utils import interval_to_timedelta

    return interval_to_timedelta(timeframe)


def _patch_binance_sandbox_urls(exchange) -> None:
    """Redirect Binance sandbox URLs from the deprecated testnet.binance.vision
    to demo-api.binance.com.

    Binance migrated Spot Testnet ("Demo Trading") to demo-api.binance.com;
    testnet.binance.vision no longer accepts authenticated requests. ccxt's
    set_sandbox_mode() hasn't been updated for this yet (ccxt/ccxt#27266,
    open as of 2026-07). Remove this patch once ccxt ships a fix upstream.
    """
    for section in ("api",):
        urls = exchange.urls.get(section)
        if not isinstance(urls, dict):
            continue
        for key, url in urls.items():
            if isinstance(url, str) and "testnet.binance.vision" in url:
                urls[key] = url.replace("testnet.binance.vision", "demo-api.binance.com")


@dataclass
class CryptoCredentials(CredentialConfig):
    """Credentials for a CCXT-backed exchange."""

    api_key: str = ""
    api_secret: str = ""
    exchange_id: str = "binance"
    sandbox: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.sandbox, str):
            self.sandbox = self.sandbox.lower() == "true"


class CryptoAdapter:
    """Crypto exchange adapter backed by CCXT.

    Parameters
    ----------
    exchange_id : str
        CCXT exchange id (default ``"binance"``).
    api_key, api_secret : str
        Exchange credentials. Empty strings → read-only mode.
    sandbox : bool
        If True, enable the exchange sandbox/testnet.
    credentials : CryptoCredentials | None
        Alternative to individual params.  When given, the explicit
        params above are ignored.
    """

    def __init__(
        self,
        exchange_id: str = "binance",
        api_key: str = "",
        api_secret: str = "",
        sandbox: bool = False,
        credentials: CryptoCredentials | None = None,
    ) -> None:
        if credentials is not None:
            exchange_id = credentials.exchange_id if credentials.exchange_id else exchange_id
            api_key = credentials.api_key if credentials.api_key else api_key
            api_secret = credentials.api_secret if credentials.api_secret else api_secret
            sandbox = credentials.sandbox or sandbox

        ccxt = _require_ccxt()
        exchange_class = getattr(ccxt, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unknown CCXT exchange: {exchange_id}")

        config: dict[str, Any] = {"enableRateLimit": True}
        if api_key:
            config["apiKey"] = api_key
        if api_secret:
            config["secret"] = api_secret

        self._exchange = exchange_class(config)
        if sandbox:
            self._exchange.set_sandbox_mode(True)
            if exchange_id == "binance":
                _patch_binance_sandbox_urls(self._exchange)

        self._read_only = not bool(api_key)
        self._exchange_id = exchange_id

    def info(self) -> AdapterInfo:
        """Return adapter metadata (consistent with ABC adapters)."""
        return AdapterInfo(
            adapter_id=f"crypto_{self._exchange_id}",
            venue=self._exchange_id.upper(),
            market_type="spot",
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 200,
        *,
        since: int | None = None,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles and return a standardised DataFrame.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT").
            timeframe: Candle interval (e.g. "1h", "1d").
            limit: Max number of candles to fetch.
            since: Start timestamp in milliseconds (CCXT convention).
            drop_incomplete: If True, drop the last candle which may still
                be forming. Essential for live monitoring to avoid computing
                indicators on partial data.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a UTC-aware ``datetime``.
        """
        raw = self._exchange.fetch_ohlcv(
            symbol,
            timeframe=timeframe,
            limit=limit,
            since=since,
        )
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)

        if len(df) < limit and since is None:
            logger.warning(
                "fetch_ohlcv returned %d bars (requested %d) for %s %s",
                len(df),
                limit,
                symbol,
                timeframe,
            )

        if drop_incomplete and len(df) > 0:
            now = datetime.now(tz=UTC)
            last_ts = df["ts"].iloc[-1]
            # WHY: if the last bar's timestamp is within the current candle
            # interval, it's still forming and should be dropped
            if last_ts.to_pydatetime() > now - _timeframe_to_delta(timeframe):
                df = df.iloc[:-1]

        return df

    def fetch_continuous_ohlcv(
        self,
        pair: str,
        contract_type: str,
        timeframe: str,
        limit: int = 200,
        *,
        since: int | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV for a Binance continuous futures contract (e.g. quarterly).

        Unlike ``fetch_ohlcv``, this queries by ``(pair, contract_type)``
        rather than a dated contract symbol (e.g. ``"BTCUSDT_260925"``) —
        Binance resolves server-side which concrete contract is currently
        ``CURRENT_QUARTER``/``NEXT_QUARTER``/``PERPETUAL``, so the caller
        never has to track the expiry date or re-register a new symbol each
        quarter as the front contract rolls. Only supported on
        ``binanceusdm``/``binancecoinm`` — this hits Binance's
        ``continuousKlines`` REST endpoint directly (fapi/dapi), not a
        general ccxt feature other exchanges expose.

        Args:
            pair: Underlying pair, e.g. ``"BTCUSDT"`` — not a dated symbol.
            contract_type: ``"PERPETUAL"``, ``"CURRENT_QUARTER"``, or
                ``"NEXT_QUARTER"``.
            timeframe: Candle interval (e.g. ``"1h"``, ``"1d"``).
            limit: Max candles per page (Binance caps at 1500).
            since: Start timestamp in milliseconds.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a UTC-aware ``datetime``.
        """
        if self._exchange_id not in ("binanceusdm", "binancecoinm"):
            raise ValueError(
                "fetch_continuous_ohlcv requires exchange_id='binanceusdm' or "
                f"'binancecoinm', got {self._exchange_id!r}"
            )
        method = (
            self._exchange.fapiPublicGetContinuousKlines
            if self._exchange_id == "binanceusdm"
            else self._exchange.dapiPublicGetContinuousKlines
        )
        params: dict[str, Any] = {
            "pair": pair,
            "contractType": contract_type,
            "interval": timeframe,
            "limit": limit,
        }
        if since is not None:
            params["startTime"] = since

        raw = method(params)
        df = pd.DataFrame(
            raw,
            columns=[
                "ts",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "close_ts",
                "quote_volume",
                "trades",
                "taker_buy_base",
                "taker_buy_quote",
                "ignore",
            ],
        )
        df = df[["ts", "open", "high", "low", "close", "volume"]].copy()
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        return df

    # ------------------------------------------------------------------
    # Order management (requires API key)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "API key not configured — CryptoAdapter is in read-only mode. "
                "Provide api_key/api_secret to enable trading."
            )

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side``, ``quantity``,
        ``order_type`` (``"market"`` or ``"limit"``), optionally ``price``
        for limit orders, and optionally ``client_order_id`` (forwarded as
        ccxt's unified ``clientOrderId`` param, exchange-side dedup/audit).
        """
        self._require_auth()
        order_type = signal.get("order_type", "market")
        price = signal.get("price")
        params = {}
        if signal.get("client_order_id"):
            params["clientOrderId"] = signal["client_order_id"]
        result = self._exchange.create_order(
            symbol=signal["symbol"],
            type=order_type,
            side=signal["side"],
            amount=signal["quantity"],
            price=price,
            params=params,
        )
        return result

    def get_balance(self, currency: str) -> dict[str, float]:
        """Return real free/used/total balance for *currency* from the exchange."""
        self._require_auth()
        balance = self._exchange.fetch_balance()
        entry = balance.get(currency) or {}
        return {
            "free": float(entry.get("free", 0) or 0),
            "used": float(entry.get("used", 0) or 0),
            "total": float(entry.get("total", 0) or 0),
        }

    def get_position(self, symbol: str) -> dict:
        """Return current position for *symbol*.

        Returns ``{symbol, size, avg_price, unrealized_pnl}``.
        """
        self._require_auth()
        positions = self._exchange.fetch_positions([symbol])
        return find_position(
            positions,
            symbol,
            matches=lambda p: p.get("symbol") == symbol,
            size=lambda p: float(p.get("contracts", 0)),
            avg_price=lambda p: float(p.get("entryPrice", 0) or 0),
            pnl=lambda p: float(p.get("unrealizedPnl", 0) or 0),
        )
