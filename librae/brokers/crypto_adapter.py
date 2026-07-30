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
from math import isfinite
from typing import Any

import pandas as pd

from librae.config.symbols import (
    AssetClass,
    AvailableSymbol,
    InstrumentKind,
)
from librae.core.utils import validate_contract_month
from librae.live.executor import PositionRequest

from .base import (
    AdapterInfo,
    CredentialConfig,
    drop_incomplete_ohlcv,
    find_position,
    validate_order_signal,
)

logger = logging.getLogger(__name__)


def _require_ccxt() -> object:
    """Import and return ccxt, raising a friendly error if missing."""
    try:
        import ccxt

        return ccxt
    except ImportError as e:
        raise ImportError(
            "CryptoAdapter requires the optional 'crypto-live' dependencies. "
            "From a repository clone run: uv sync --extra crypto-live. "
            "For a direct install, include Librae's 'crypto-live' extra."
        ) from e


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

    def available_symbols(
        self,
        *,
        query: str | None = None,
        kind: InstrumentKind | None = None,
        asset_class: AssetClass | None = None,
    ) -> tuple[AvailableSymbol, ...]:
        """List active CCXT markets using current exchange metadata."""
        markets = self._exchange.load_markets()
        if not isinstance(markets, dict):
            raise ValueError(f"{self._exchange_id} load_markets() did not return a mapping")
        query_token = "".join(
            character for character in (query or "").upper() if character.isalnum()
        )
        results: list[AvailableSymbol] = []
        for market in markets.values():
            if not isinstance(market, dict) or market.get("active") is False:
                continue
            if market.get("spot") or market.get("type") == "spot":
                resolved_kind: InstrumentKind = "spot"
                instrument_type = "spot"
            elif market.get("swap") or market.get("type") == "swap":
                resolved_kind = "perpetual"
                instrument_type = "contract_perpetual"
            elif market.get("future") or market.get("type") == "future":
                resolved_kind = "future"
                instrument_type = "contract_quarterly"
            else:
                continue
            if kind is not None and resolved_kind != kind:
                continue

            info = market.get("info") or {}
            if not isinstance(info, dict):
                info = {}
            underlying_type = str(info.get("underlyingType") or "").upper()
            if underlying_type in ("EQUITY", "HK_EQUITY"):
                resolved_asset_class: AssetClass = "equity"
            elif underlying_type == "INDEX":
                resolved_asset_class = "index"
            else:
                resolved_asset_class = "crypto"
            if asset_class is not None and resolved_asset_class != asset_class:
                continue

            venue_symbol = str(market.get("symbol") or "")
            native_symbol = str(market.get("id") or venue_symbol)
            base = str(market.get("base") or "")
            quote = str(market.get("quote") or "")
            search_tokens = {
                "".join(character for character in value.upper() if character.isalnum())
                for value in (
                    venue_symbol,
                    native_symbol,
                    base,
                    f"{base}{quote}",
                )
            }
            if query_token and query_token not in search_tokens:
                continue

            expiry = market.get("expiry")
            delivery_month = None
            if isinstance(expiry, (int, float)) and not isinstance(expiry, bool):
                delivery_month = pd.to_datetime(expiry, unit="ms", utc=True).strftime("%Y%m")
            contract_type = str(info.get("contractType") or "")
            if resolved_kind == "future" and "QUARTER" not in contract_type:
                instrument_type = "contract_monthly"
            contract_rank = {
                "CURRENT_QUARTER": 0,
                "NEXT_QUARTER": 1,
            }.get(contract_type)
            if resolved_kind == "spot":
                canonical_symbol = f"{base}{quote}_SPOT"
                multiplier = 1.0
            elif resolved_kind == "perpetual":
                canonical_symbol = f"{base}{quote}_PERP"
                multiplier = market.get("contractSize")
            else:
                canonical_symbol = native_symbol
                multiplier = market.get("contractSize")
            tick_size = (market.get("precision") or {}).get("price")
            currency = str(market.get("settle") or quote)
            results.append(
                AvailableSymbol(
                    broker="binance",
                    canonical_symbol=canonical_symbol,
                    venue_symbol=venue_symbol,
                    native_symbol=native_symbol,
                    name=str(info.get("pair") or native_symbol),
                    kind=resolved_kind,
                    asset_class=resolved_asset_class,
                    currency=currency,
                    instrument_type=instrument_type,
                    contract_month=delivery_month,
                    delivery_month=delivery_month,
                    contract_rank=contract_rank,
                    multiplier=float(multiplier) if multiplier is not None else None,
                    tick_size=float(tick_size) if tick_size is not None else None,
                )
            )
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    item.kind,
                    item.canonical_symbol,
                    item.delivery_month or "",
                ),
            )
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
        continuous_alias: bool = False,
        contract_month: str | None = None,
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
            continuous_alias: Unsupported for this CCXT path; continuous
                klines are research data, not an orderable identity.
            contract_month: Expected ``YYYYMM`` for a delivery-future symbol.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is the UTC-aware bar-start ``datetime``.
        """
        if continuous_alias or contract_month is not None:
            self._exchange.load_markets()
            self._validate_contract_selection(
                symbol,
                self._exchange.market(symbol),
                continuous_alias=continuous_alias,
                contract_month=contract_month,
            )
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
            df = drop_incomplete_ohlcv(df, timeframe)

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

    def prepare_order(self, signal: dict) -> dict:
        """Apply CCXT precision/limit rules and reject spot short opens."""
        validate_order_signal(signal)
        self._exchange.load_markets()
        symbol = signal["symbol"]
        market = self._exchange.market(symbol)
        self._validate_contract_selection(
            symbol,
            market,
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        prepared = dict(signal)

        quantity = float(self._exchange.amount_to_precision(symbol, signal["quantity"]))
        if quantity <= 0:
            raise ValueError(f"{symbol} quantity rounds to zero")
        prepared["quantity"] = quantity

        price = signal.get("price")
        if price is not None:
            price = float(self._exchange.price_to_precision(symbol, price))
            if price <= 0:
                raise ValueError(f"{symbol} price rounds to zero")
            prepared["price"] = price

        is_spot = bool(market.get("spot") or market.get("type") == "spot")
        if (
            is_spot
            and signal["side"] == "sell"
            and signal.get("position_effect") in ("open", "add")
        ):
            raise ValueError(f"{symbol} spot inventory cannot open a short position")

        limits = market.get("limits") or {}
        self._validate_limit(quantity, limits.get("amount"), "quantity", symbol)
        if price is not None:
            self._validate_limit(price, limits.get("price"), "price", symbol)
        reference_price = price or signal.get("reference_price")
        if reference_price is not None:
            raw_contract_size = market.get("contractSize")
            is_contract = bool(
                market.get("contract") or market.get("type") in ("future", "swap", "option")
            )
            if is_contract and raw_contract_size is None:
                raise ValueError(f"{symbol} derivative is missing contractSize")
            contract_size = float(raw_contract_size or 1.0)
            if contract_size <= 0:
                raise ValueError(f"{symbol} contractSize must be positive")
            notional = quantity * float(reference_price) * contract_size
            self._validate_limit(notional, limits.get("cost"), "notional", symbol)
        return prepared

    @staticmethod
    def _validate_limit(
        value: float,
        limits: dict | None,
        name: str,
        symbol: str,
    ) -> None:
        if not limits:
            return
        minimum = limits.get("min")
        maximum = limits.get("max")
        if minimum is not None and value < float(minimum):
            raise ValueError(f"{symbol} {name} {value} is below minimum {minimum}")
        if maximum is not None and value > float(maximum):
            raise ValueError(f"{symbol} {name} {value} exceeds maximum {maximum}")

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side``, ``quantity``,
        ``order_type`` (``"market"`` or ``"limit"``), optionally ``price``
        for limit orders, and optionally ``client_order_id`` (forwarded as
        ccxt's unified ``clientOrderId`` param, exchange-side dedup/audit).
        """
        self._require_auth()
        validate_order_signal(signal)
        self._exchange.load_markets()
        self._validate_contract_selection(
            signal["symbol"],
            self._exchange.market(signal["symbol"]),
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        order_type = signal["order_type"]
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

    def find_order(self, client_order_id: str, symbol: str) -> dict | None:
        """Find an open or final order after an ambiguous create response."""
        self._require_auth()
        if self._exchange.has.get("fetchOrders"):
            orders = self._exchange.fetch_orders(symbol)
        elif self._exchange.has.get("fetchOpenOrders") and self._exchange.has.get(
            "fetchClosedOrders"
        ):
            orders = self._exchange.fetch_open_orders(symbol)
            orders.extend(self._exchange.fetch_closed_orders(symbol))
        else:
            raise NotImplementedError(
                f"{self._exchange_id} cannot look up both open and final orders; "
                "durable live execution is unsupported"
            )
        matches = [
            order for order in orders if str(order.get("clientOrderId") or "") == client_order_id
        ]
        if len(matches) > 1:
            raise ValueError(f"duplicate broker clientOrderId: {client_order_id}")
        return matches[0] if matches else None

    def get_order(self, order_id: str, symbol: str) -> dict:
        """Return the latest cumulative CCXT order state."""
        self._require_auth()
        if not self._exchange.has.get("fetchOrder"):
            raise NotImplementedError(f"{self._exchange_id} does not support fetchOrder")
        return self._exchange.fetch_order(order_id, symbol)

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Return currently resting orders for orphan detection."""
        self._require_auth()
        if not self._exchange.has.get("fetchOpenOrders"):
            raise NotImplementedError(f"{self._exchange_id} does not support fetchOpenOrders")
        return self._exchange.fetch_open_orders(symbol)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an order, then fetch its cumulative terminal state."""
        self._require_auth()
        if not self._exchange.has.get("cancelOrder"):
            raise NotImplementedError(f"{self._exchange_id} does not support cancelOrder")
        self._exchange.cancel_order(order_id, symbol)
        return self.get_order(order_id, symbol)

    def get_balance(self, currency: str) -> dict[str, float]:
        """Return real free/used/total balance for *currency* from the exchange."""
        self._require_auth()
        balance = self._exchange.fetch_balance()
        entry = balance.get(currency)
        if entry is None:
            # CCXT omits currencies with no balance; that absence means zero,
            # unlike a present-but-incomplete balance record.
            return {"free": 0.0, "used": 0.0, "total": 0.0}
        missing = [field for field in ("free", "used", "total") if entry.get(field) is None]
        if missing:
            raise ValueError(f"{currency} balance is missing fields: {', '.join(missing)}")
        return {
            "free": float(entry["free"]),
            "used": float(entry["used"]),
            "total": float(entry["total"]),
        }

    def get_position(self, request: PositionRequest) -> dict:
        """Return the current position for one configured instrument.

        Returns ``{symbol, size, avg_price, unrealized_pnl}``.
        """
        self._require_auth()
        symbol = request.venue_symbol
        self._exchange.load_markets()
        market = self._exchange.market(symbol)
        self._validate_contract_selection(
            symbol,
            market,
            continuous_alias=request.continuous_alias,
            contract_month=request.contract_month,
        )
        if market.get("spot") or market.get("type") == "spot":
            balance = self._exchange.fetch_balance()
            base = market["base"]
            entry = balance.get(base) or {}
            total = entry.get("total")
            if total is None and isinstance(balance.get("total"), dict):
                total = balance["total"].get(base)
            if total is None:
                free = entry.get("free")
                used = entry.get("used")
                if free is None or used is None:
                    raise ValueError(f"{symbol} balance is missing total and free/used")
                total = float(free) + float(used)
            return {
                "symbol": request.symbol,
                "size": float(total),
                "avg_price": None,
                "unrealized_pnl": 0.0,
            }

        positions = self._exchange.fetch_positions([symbol])

        def signed_contracts(position: dict) -> float:
            if position.get("contracts") is None:
                raise ValueError(f"{symbol} derivative position is missing contracts")
            contracts = float(position["contracts"])
            if not isfinite(contracts) or contracts < 0:
                raise ValueError(f"{symbol} derivative position has invalid contracts")
            if contracts == 0:
                return 0.0
            side = position.get("side")
            if side == "long":
                return contracts
            if side == "short":
                return -contracts
            raise ValueError(f"{symbol} derivative position has unsupported side: {side!r}")

        def entry_price(position: dict) -> float:
            if position.get("entryPrice") is None:
                raise ValueError(f"{symbol} derivative position is missing entryPrice")
            return float(position["entryPrice"])

        return find_position(
            positions,
            request.symbol,
            matches=lambda p: p.get("symbol") == symbol,
            size=signed_contracts,
            avg_price=entry_price,
            pnl=lambda p: float(p.get("unrealizedPnl", 0) or 0),
        )

    @staticmethod
    def _validate_contract_selection(
        symbol: str,
        market: dict,
        *,
        continuous_alias: bool,
        contract_month: str | None,
    ) -> None:
        """Verify the common contract identity against CCXT market metadata."""
        if not isinstance(continuous_alias, bool):
            raise TypeError("continuous_alias must be a bool")
        contract_month = validate_contract_month(contract_month)
        if continuous_alias:
            raise ValueError(
                "CryptoAdapter does not order continuous aliases; "
                "configure an exact CCXT delivery symbol"
            )

        is_delivery_future = bool(market.get("future") or market.get("type") == "future")
        if contract_month is None:
            if is_delivery_future:
                raise ValueError(f"{symbol} delivery future requires contract_month='YYYYMM'")
            return
        if not is_delivery_future:
            raise ValueError(f"{symbol} contract_month is valid only for a CCXT delivery future")
        raw_expiry = market.get("expiry")
        if isinstance(raw_expiry, bool) or not isinstance(raw_expiry, (int, float)):
            raise ValueError(f"{symbol} delivery future has no numeric CCXT expiry")
        expiry = float(raw_expiry)
        if not isfinite(expiry):
            raise ValueError(f"{symbol} delivery future has invalid CCXT expiry")
        resolved_month = pd.to_datetime(expiry, unit="ms", utc=True).strftime("%Y%m")
        if resolved_month != contract_month:
            raise ValueError(
                f"CCXT contract month mismatch for {symbol}: "
                f"configured={contract_month}, broker={resolved_month}"
            )
