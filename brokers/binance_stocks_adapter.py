"""Binance Stocks market-data client.

Binance Stocks is a separate SAPI product from Binance crypto Spot and
USD-M Futures.  Its symbol catalog uses plain US-equity tickers and is not
available through CCXT's ``load_markets()``.

The current official REST API exposes symbol discovery and latest quotes, but
not historical OHLCV.  This class therefore intentionally implements only the
observed market-data capabilities instead of pretending to satisfy Librae's
bar-based live adapter contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from librae.config.symbols import AssetClass, AvailableSymbol, InstrumentKind

from .base import AdapterInfo, CredentialConfig

BINANCE_STOCKS_API_SCHEMA_VERSION = "1.0.0"
_DEFAULT_BASE_URL = "https://api.binance.com"
_EXCHANGE_INFO_PATH = "/sapi/v1/equity/market/exchangeInfo"
_LATEST_QUOTE_PATH = "/sapi/v1/equity/market/quote"


@dataclass
class BinanceStocksCredentials(CredentialConfig):
    """Credentials shared with the user's Binance account."""

    api_key: str = ""
    api_secret: str = ""


class BinanceStocksAdapter:
    """Read the official Binance Stocks catalog and latest quotes."""

    def __init__(
        self,
        *,
        api_key: str = "",
        credentials: BinanceStocksCredentials | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        if credentials is not None and credentials.api_key:
            api_key = credentials.api_key
        if not api_key:
            raise ValueError(
                "Binance Stocks market data requires BINANCE_API_KEY; "
                "the equity SAPI exchangeInfo endpoint is not anonymous"
            )
        self._headers = {"X-MBX-APIKEY": api_key}
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=10.0,
        )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="binance_stocks",
            venue="BINANCE",
            market_type="us_equity",
            schema_version=BINANCE_STOCKS_API_SCHEMA_VERSION,
        )

    def available_symbols(
        self,
        *,
        query: str | None = None,
        kind: InstrumentKind | None = None,
        asset_class: AssetClass | None = None,
    ) -> tuple[AvailableSymbol, ...]:
        """Return current Binance Stocks tickers from equity exchangeInfo."""
        if kind not in (None, "spot"):
            raise ValueError("BinanceStocksAdapter supports kind='spot' only")
        if asset_class not in (None, "equity"):
            return ()

        query_symbol = query.strip().upper() if query is not None else None
        if query_symbol == "":
            raise ValueError("Binance Stocks query must be non-empty when supplied")
        payload = self._get_json(
            _EXCHANGE_INFO_PATH,
            params={"symbol": query_symbol} if query_symbol is not None else None,
        )
        raw_symbols = payload.get("symbols")
        if not isinstance(raw_symbols, list):
            raise ValueError("Binance Stocks exchangeInfo response is missing symbols[]")

        results: list[AvailableSymbol] = []
        for raw in raw_symbols:
            if not isinstance(raw, Mapping):
                raise ValueError("Binance Stocks exchangeInfo symbols[] must contain objects")
            symbol = str(raw.get("symbol") or "")
            if not symbol:
                raise ValueError("Binance Stocks exchangeInfo returned an empty symbol")
            if query_symbol is not None and symbol != query_symbol:
                raise ValueError(
                    "Binance Stocks exchangeInfo returned a symbol that does not match "
                    f"the exact query: expected {query_symbol!r}, got {symbol!r}"
                )
            name = str(
                raw.get("name")
                or raw.get("displayName")
                or raw.get("description")
                or symbol
            )
            results.append(
                AvailableSymbol(
                    broker="binance",
                    canonical_symbol=symbol,
                    venue_symbol=symbol,
                    native_symbol=symbol,
                    name=name,
                    kind="spot",
                    asset_class="equity",
                    currency="USD",
                    instrument_type="spot",
                    security_type="STK",
                    exchange=(
                        str(raw["exchange"]) if raw.get("exchange") is not None else None
                    ),
                    multiplier=1.0,
                )
            )
        return tuple(sorted(results, key=lambda item: item.canonical_symbol))

    def fetch_quote(self, symbol: str) -> dict[str, Any]:
        """Return the official latest-quote payload for one exact ticker."""
        ticker = symbol.strip().upper()
        if not ticker:
            raise ValueError("Binance Stocks quote symbol must be non-empty")
        payload = self._get_json(_LATEST_QUOTE_PATH, params={"symbol": ticker})
        if not payload:
            raise ValueError(f"Binance Stocks returned no quote for {ticker!r}")
        returned_symbol = payload.get("symbol")
        if returned_symbol is not None and returned_symbol != ticker:
            raise ValueError(
                "Binance Stocks quote symbol mismatch: "
                f"expected {ticker!r}, got {returned_symbol!r}"
            )
        return dict(payload)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self._client.get(path, params=params, headers=self._headers)
        response.raise_for_status()
        if not response.content:
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError(f"Binance Stocks {path} response must be an object")
        return payload
