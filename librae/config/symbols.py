"""Per-symbol registry — symbol -> market + data source + contract economics.

Market-level cost params (commission/tax/slippage/margin, plus an
approximate default tick_size — see market_config.py) are genuinely shared
across every instrument in a market and live there (MarketConfig).

multiplier does NOT get a market-level fallback: for 'spot' instruments
it's a mathematical invariant (buying spot = 1 unit for 1 unit) and
defaults to 1.0 automatically here; for any contract_* instrument it
varies per contract (TXF=200 vs MXF=50 vs TMF=10, all "market: tw_futures")
and must be declared explicitly, no default — mirrors how mainstream
frameworks handle this (e.g. QuantConnect LEAN's
symbol-properties-database.csv: a market-wide wildcard row for equities,
an explicit row per specific futures contract).

This registry used to live in a bundled symbols.yaml; it's a plain Python
dict now — that file was never actually included in the built wheel (only
.py files are, without extra packaging config), so `pip install librae`
raised FileNotFoundError the moment get_symbol() ran for any built-in
symbol. A handful of hardcoded entries needs no parser, no packaging
config, and can't go missing from the wheel.

Registering your own symbol doesn't require editing this file. Cost fields
belong in RunConfig.symbol_cost_overrides; venue/data fields belong in
RunConfig.instrument_overrides. Execution brokerage is deliberately absent
from the registry and must be selected by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from librae.core.run_config import RunConfig

from librae.core.utils import validate_contract_month

type AdapterName = str
type BrokerName = str
InstrumentKind = Literal["spot", "perpetual", "future"]
AssetClass = Literal["crypto", "equity", "index", "commodity", "fx", "rate", "unknown"]
_ADAPTER_BY_DATA_SOURCE: dict[str, AdapterName] = {
    "binance_spot": "crypto",
    "binance_futures_continuous": "crypto",
    "ibkr": "ibkr",
    "shioaji": "shioaji",
}
# Contract expiry structure — orthogonal to continuous_alias (see the
# per-symbol entries below). 'spot' is the bare case (direct ownership, not
# an exchange-traded derivative); everything else is prefixed contract_* so
# the derivative family is filterable/greppable as a group, and so the
# multiplier-defaulting rule below (spot=1.0 automatic, contract_*
# explicit-required) can key off the prefix. Extend this set (and the
# matching DB CHECK constraint in librae/db/timescale_init.sql) when a new type is
# actually needed — don't pre-enumerate speculative ones.
#
# This is the single source of truth for the value — librae/artifacts.py and
# librae/db/timescale_writer.py both call validate_instrument_type() below
# instead of re-declaring the set, so a caller who never touches TimescaleDB
# (Parquet/SQLite/no persistence at all) still gets it rejected here instead
# of silently writing a value only the Postgres CHECK constraint would catch.
ALLOWED_INSTRUMENT_TYPES = frozenset(
    {
        "spot",
        "contract_perpetual",
        "contract_monthly",
        "contract_quarterly",
    }
)


def validate_instrument_type(instrument_type: str, *, context: str = "instrument_type") -> None:
    """Raise ValueError unless instrument_type is one of ALLOWED_INSTRUMENT_TYPES."""
    if instrument_type not in ALLOWED_INSTRUMENT_TYPES:
        raise ValueError(
            f"{context}={instrument_type!r} is not one of {sorted(ALLOWED_INSTRUMENT_TYPES)}"
        )


@dataclass(frozen=True)
class AvailableSymbol:
    """One live broker-catalog result, separate from configured accounting.

    ``venue_symbol`` is the value Librae's adapter accepts. ``native_symbol``
    is the venue's resolved display identifier when it differs (for example,
    an IBKR futures local symbol while the adapter routes by root + month).
    ``contract_rank`` is informational: 0 is the currently nearest listed
    contract and 1 is the next one. A ranked exact contract remains pinned to
    ``contract_month``; it does not silently become a rolling alias.
    """

    broker: BrokerName
    canonical_symbol: str
    venue_symbol: str
    native_symbol: str
    name: str
    kind: InstrumentKind
    asset_class: AssetClass
    currency: str
    instrument_type: str
    security_type: str | None = None
    exchange: str | None = None
    contract_month: str | None = None
    delivery_month: str | None = None
    contract_rank: int | None = None
    continuous_alias: bool = False
    multiplier: float | None = None
    tick_size: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.broker, str) or not self.broker:
            raise ValueError("broker must be a non-empty string")
        if self.kind not in ("spot", "perpetual", "future"):
            raise ValueError(f"unsupported instrument kind: {self.kind!r}")
        if self.asset_class not in (
            "crypto",
            "equity",
            "index",
            "commodity",
            "fx",
            "rate",
            "unknown",
        ):
            raise ValueError(f"unsupported asset_class: {self.asset_class!r}")
        if not all(
            isinstance(value, str) and value
            for value in (
                self.canonical_symbol,
                self.venue_symbol,
                self.native_symbol,
                self.name,
                self.currency,
            )
        ):
            raise ValueError("available-symbol text fields must be non-empty")
        validate_instrument_type(self.instrument_type)
        validate_contract_month(self.contract_month)
        validate_contract_month(self.delivery_month)
        if self.contract_rank is not None and (
            isinstance(self.contract_rank, bool)
            or not isinstance(self.contract_rank, int)
            or self.contract_rank < 0
        ):
            raise ValueError("contract_rank must be a non-negative integer or None")
        if not isinstance(self.continuous_alias, bool):
            raise TypeError("continuous_alias must be a bool")
        if self.continuous_alias and self.contract_month is not None:
            raise ValueError("continuous alias cannot also pin contract_month")
        for field_name in ("multiplier", "tick_size"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be positive when supplied")

    def market_data_kwargs(self) -> dict[str, object]:
        """Return verified routing kwargs for the concrete adapter."""
        if self.security_type == "STK" and self.broker == "binance":
            return {}
        kwargs: dict[str, object] = {
            "continuous_alias": self.continuous_alias,
            "contract_month": self.contract_month,
        }
        if self.broker == "ibkr":
            kwargs.update(
                security_type=self.security_type,
                exchange=self.exchange,
                currency=self.currency,
            )
        return kwargs

    def instrument_override(self) -> dict[str, object]:
        """Return the non-accounting portion of a RunConfig symbol route."""
        if self.security_type == "STK" and self.broker == "binance":
            raise ValueError(
                "Binance Stocks REST does not provide historical OHLCV and is not "
                "a built-in bar/live RunConfig adapter; use fetch_quote() or inject "
                "an explicit historical bar source"
            )
        adapter: AdapterName = "crypto" if self.broker == "binance" else self.broker
        route: dict[str, object] = {
            "broker": self.broker,
            "data_adapter": adapter,
            "venue_symbol": self.venue_symbol,
            "currency": self.currency,
            "instrument_type": self.instrument_type,
            "continuous_alias": self.continuous_alias,
        }
        for key in ("security_type", "exchange", "contract_month"):
            value = getattr(self, key)
            if value is not None:
                route[key] = value
        return route

    def cost_override(self) -> dict[str, float]:
        """Return broker-verified contract economics for RunConfig."""
        if self.multiplier is None:
            raise ValueError(f"{self.native_symbol} catalog metadata has no contract multiplier")
        costs = {"multiplier": self.multiplier}
        if self.tick_size is not None:
            costs["tick_size"] = self.tick_size
        return costs


@dataclass(frozen=True)
class SymbolInfo:
    """Registry entry for a single symbol.

    ``calendar_id`` is the sole trading-session identity and is intentionally
    separate from a display timezone or vendor timestamp correction.

    multiplier is always populated once loaded (never None) — either from
    an explicit value, or auto-resolved to 1.0 for 'spot' (see
    _build_registry). tick_size is optional; None means "use
    market_config.py's market-level default" (an acceptable approximation
    — see this module's docstring for why tick_size and multiplier have
    different risk profiles).
    """

    symbol: str
    market: str
    data_source: str
    instrument_type: str
    multiplier: float
    data_adapter: AdapterName
    venue_symbol: str
    currency: str
    continuous_alias: bool = False
    contract_month: str | None = None
    tick_size: float | None = None
    security_type: str | None = None
    exchange: str | None = None
    calendar_id: str | None = None

    def __post_init__(self) -> None:
        validate_instrument_type(self.instrument_type, context=f"{self.symbol!r} instrument_type")
        if not isinstance(self.data_adapter, str) or not self.data_adapter:
            raise ValueError(f"{self.symbol!r} data_adapter must be a non-empty string")
        if self.multiplier <= 0:
            raise ValueError(f"{self.symbol!r} multiplier must be positive")
        if not self.venue_symbol:
            raise ValueError(f"{self.symbol!r} venue_symbol must be non-empty")
        if not self.currency:
            raise ValueError(f"{self.symbol!r} currency must be non-empty")
        if self.calendar_id is not None and not self.calendar_id:
            raise ValueError(f"{self.symbol!r} calendar_id must be non-empty or None")
        if not isinstance(self.continuous_alias, bool):
            raise TypeError(f"{self.symbol!r} continuous_alias must be a bool")
        validate_contract_month(self.contract_month)
        dated_contract = self.instrument_type in (
            "contract_monthly",
            "contract_quarterly",
        )
        if dated_contract and self.continuous_alias == (self.contract_month is not None):
            raise ValueError(
                f"{self.symbol!r} dated contract requires exactly one of "
                "continuous_alias=True or contract_month='YYYYMM'"
            )
        if not dated_contract and self.contract_month is not None:
            raise ValueError(
                f"{self.symbol!r} contract_month is valid only for monthly/quarterly contracts"
            )


def _build_registry(raw: dict[str, dict]) -> dict[str, SymbolInfo]:
    """Validate + construct a symbol registry from a plain raw dict.

    Raises:
        ValueError: instrument_type is missing/invalid, or a contract_*
            instrument is missing 'multiplier' — caught here, at build
            time, rather than letting an unvalidated/defaulted value drift
            into a PnL calculation. 'spot' instruments don't need
            'multiplier' at all (defaults to 1.0).
    """
    registry: dict[str, SymbolInfo] = {}
    for symbol, data in raw.items():
        instrument_type = str(data.get("instrument_type", ""))
        raw_multiplier = data.get("multiplier")
        if raw_multiplier is not None:
            multiplier = float(raw_multiplier)
        elif instrument_type == "spot":
            multiplier = 1.0
        else:
            raise ValueError(
                f"{symbol!r} (instrument_type={instrument_type!r}) is missing "
                "'multiplier' — only 'spot' gets a safe automatic default (1.0); "
                "contract_* instruments vary per contract and must declare it explicitly."
            )
        raw_tick_size = data.get("tick_size")
        registry[symbol] = SymbolInfo(
            symbol=symbol,
            market=str(data.get("market", "")),
            data_source=str(data.get("data_source", "")),
            instrument_type=instrument_type,
            multiplier=multiplier,
            data_adapter=str(data["data_adapter"]),
            venue_symbol=str(data.get("venue_symbol", symbol)),
            currency=str(data["currency"]),
            continuous_alias=data.get("continuous_alias", False),
            contract_month=validate_contract_month(data.get("contract_month")),
            tick_size=float(raw_tick_size) if raw_tick_size is not None else None,
            security_type=(
                str(data["security_type"]) if data.get("security_type") is not None else None
            ),
            exchange=str(data["exchange"]) if data.get("exchange") is not None else None,
            calendar_id=(str(data["calendar_id"]) if data.get("calendar_id") is not None else None),
        )
    return registry


# Built-in reference symbols — see this module's docstring for why these
# are a plain dict rather than a bundled YAML file.
_BUILTIN_SYMBOLS: dict[str, SymbolInfo] = _build_registry(
    {
        "BTCUSDT": {
            "market": "crypto",
            "data_source": "binance_spot",
            "instrument_type": "spot",
            "data_adapter": "crypto",
            "venue_symbol": "BTC/USDT",
            "currency": "USDT",
            "calendar_id": "24/7",
            # multiplier/tick_size omitted on purpose — spot auto-defaults
            # to multiplier=1.0, tick_size falls back to market_config.py's
            # crypto default (0.01).
        },
        "BTCUSDT_QUARTERLY": {
            "market": "crypto",
            "data_source": "binance_futures_continuous",
            "instrument_type": "contract_quarterly",
            "data_adapter": "crypto",
            "venue_symbol": "BTC/USDT:USDT",
            "currency": "USDT",
            "calendar_id": "24/7",
            "continuous_alias": True,
            # Binance USDT-M linear contract, contractSize=1 BTC per
            # contract (verified via ccxt binanceusdm market info) — 1
            # contract == 1 BTC notional.
            "multiplier": 1.0,
        },
        "TXFR1": {
            "market": "tw_futures",
            "data_source": "shioaji",
            "instrument_type": "contract_monthly",
            "data_adapter": "shioaji",
            "currency": "TWD",
            "calendar_id": "XTAIFEX",
            "continuous_alias": True,
            "multiplier": 200.0,  # 臺股期貨（大台）— required, no safe default for contract_* types
            "tick_size": 1.0,  # 1 index point; venue price limits remain contract-specific
        },
        "MXFR1": {
            "market": "tw_futures",
            "data_source": "shioaji",
            "instrument_type": "contract_monthly",
            "data_adapter": "shioaji",
            "currency": "TWD",
            "calendar_id": "XTAIFEX",
            "continuous_alias": True,
            "multiplier": 50.0,  # 小型臺指期貨（小台）— TAIFEX 契約規格：指數 x 50 元
            "tick_size": 1.0,
        },
        "TMFR1": {
            "market": "tw_futures",
            "data_source": "shioaji",
            "instrument_type": "contract_monthly",
            "data_adapter": "shioaji",
            "currency": "TWD",
            "calendar_id": "XTAIFEX",
            "continuous_alias": True,
            "multiplier": 10.0,  # 微型臺指期貨（微台）— TAIFEX 契約規格：指數 x 10 元
            "tick_size": 1.0,
        },
        "MU": {
            "market": "us_equity",
            "data_source": "ibkr",
            "instrument_type": "spot",
            "data_adapter": "ibkr",
            "currency": "USD",
            "security_type": "STK",
            "calendar_id": "XNYS",
            # multiplier/tick_size omitted — spot auto-defaults to
            # multiplier=1.0, tick_size falls back to market_config.py's
            # us_equity default (0.01).
        },
    }
)


# Not registered yet — reference values for when they're actually added:
#   個股 (individual TW stocks): market: tw_equity, instrument_type: spot —
#     multiplier auto-defaults to 1.0 like any spot instrument, no per-stock
#     registration needed. tick_size does vary by price band in Taiwan
#     (NT$10 以下 0.01、10-50 0.05、50-100 0.1...) — if that precision is
#     ever needed, it's a function of price, not a single per-symbol
#     constant; don't build that machinery until an actual stock strategy
#     needs it (market_config.py's approximate default is fine until then).
#
#   MUUSDT (Binance TradFi perpetual tracking MU's price, contractType
#     TRADIFI_PERPETUAL/underlyingType EQUITY — confirmed via fapi
#     exchangeInfo 2026-07-20): would need its own data_source fetcher
#     first (get_ohlcv's binance_spot/binance_futures_continuous fetchers
#     don't cover this contract family) before it can be registered here.


def load_symbol_registry() -> dict[str, SymbolInfo]:
    """Return the built-in symbol registry (a copy — callers can't mutate it)."""
    return dict(_BUILTIN_SYMBOLS)


def get_symbol(symbol: str) -> SymbolInfo:
    """Get a single symbol's registry entry by name."""
    if symbol not in _BUILTIN_SYMBOLS:
        available = list(_BUILTIN_SYMBOLS.keys())
        raise KeyError(f"Symbol '{symbol}' not found. Available: {available}")
    return _BUILTIN_SYMBOLS[symbol]


def available_symbols(
    broker: BrokerName,
    *,
    query: str | None = None,
    kind: InstrumentKind | None = None,
    asset_class: AssetClass | None = None,
    exchange: str | None = None,
    currency: str = "USD",
    adapter: object | None = None,
) -> tuple[AvailableSymbol, ...]:
    """Query the broker's current catalog instead of a static symbol list.

    Binance discovery is public. Shioaji discovery logs in when no adapter is
    supplied; IBKR discovery opens a read-only TWS/Gateway connection. Passing
    an existing adapter reuses its authenticated session.
    """
    if broker not in ("binance", "ibkr", "shioaji"):
        raise ValueError(f"unsupported broker: {broker!r}")
    if kind not in (None, "spot", "perpetual", "future"):
        raise ValueError(f"unsupported instrument kind: {kind!r}")
    if asset_class not in (
        None,
        "crypto",
        "equity",
        "index",
        "commodity",
        "fx",
        "rate",
        "unknown",
    ):
        raise ValueError(f"unsupported asset_class: {asset_class!r}")

    if broker == "binance" and kind is None:
        raise ValueError(
            "Binance available_symbols requires kind='spot', 'perpetual', or 'future'; "
            "its crypto, derivatives, and Stocks catalogs are separate products"
        )
    if broker == "binance" and kind == "spot" and asset_class is None:
        raise ValueError(
            "Binance spot discovery requires asset_class='crypto' or 'equity' "
            "to select the correct product catalog"
        )

    created_adapter = adapter is None
    if adapter is None:
        if broker == "binance" and kind == "spot" and asset_class == "equity":
            from librae.brokers.binance_stocks_adapter import (
                BinanceStocksAdapter,
                BinanceStocksCredentials,
            )

            credentials = BinanceStocksCredentials.from_env("BINANCE")
            adapter = BinanceStocksAdapter(credentials=credentials)
        elif broker == "binance":
            from librae.brokers.crypto_adapter import CryptoAdapter

            adapter = CryptoAdapter(exchange_id="binance")
        elif broker == "shioaji":
            from librae.brokers.shioaji_adapter import ShioajiAdapter

            adapter = ShioajiAdapter()
        else:
            from librae.brokers.ibkr_adapter import IBKRAdapter

            adapter = IBKRAdapter()

    try:
        method = getattr(adapter, "available_symbols", None)
        if not callable(method):
            raise TypeError(f"{broker} adapter does not implement available_symbols")
        if broker == "binance":
            return method(query=query, kind=kind, asset_class=asset_class)
        if query is None or not query.strip():
            raise ValueError(f"{broker} available_symbols requires query")
        if broker == "shioaji":
            return method(
                query=query,
                kind=kind or "future",
                asset_class=asset_class,
            )
        if kind is None:
            raise ValueError("IBKR available_symbols requires kind='spot' or 'future'")
        return method(
            query=query,
            kind=kind,
            asset_class=asset_class,
            exchange=exchange,
            currency=currency,
        )
    finally:
        if created_adapter:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()


def resolve_symbol(
    config: RunConfig,
    symbol: str,
    *,
    multiplier: float | None = None,
) -> SymbolInfo:
    """Resolve accounting and broker metadata for one configured symbol.

    Registry values are authoritative for registered symbols. Run-wide
    market/data_source values are fallbacks for homogeneous, unregistered
    universes; ``instrument_overrides`` supplies per-symbol routing metadata.
    """
    registered = _BUILTIN_SYMBOLS.get(symbol)
    route = (config.instrument_overrides or {}).get(symbol, {})
    costs = dict(config.cost_overrides or {})
    costs.update((config.symbol_cost_overrides or {}).get(symbol, {}))

    market = route.get("market") or (registered.market if registered else config.market)
    data_source = route.get("data_source") or (
        registered.data_source if registered else config.data_source
    )
    data_adapter = route.get("data_adapter") or (
        registered.data_adapter if registered else _ADAPTER_BY_DATA_SOURCE.get(data_source)
    )
    if not isinstance(data_adapter, str) or not data_adapter:
        raise ValueError(
            f"No data adapter route for symbol={symbol!r}, data_source={data_source!r}; "
            "set instrument_overrides[symbol]['data_adapter']"
        )

    multiplier = (
        multiplier
        if multiplier is not None
        else costs.get("multiplier", registered.multiplier if registered else None)
    )
    if multiplier is None:
        raise ValueError(
            f"No multiplier for symbol={symbol!r}; set symbol_cost_overrides[symbol]['multiplier']"
        )
    instrument_type = route.get("instrument_type") or (
        registered.instrument_type if registered else ""
    )
    if not instrument_type:
        raise ValueError(
            f"No instrument_type for symbol={symbol!r}; set "
            "instrument_overrides[symbol]['instrument_type']"
        )
    tick_size = costs.get("tick_size", registered.tick_size if registered else None)
    currency = route.get("currency") or (registered.currency if registered else "")
    if not currency:
        raise ValueError(
            f"No currency for symbol={symbol!r}; set instrument_overrides[symbol]['currency']"
        )
    if "account_id" in route:
        raise ValueError(
            f"instrument_overrides[{symbol!r}].account_id is not supported; "
            "one run owns exactly RunConfig.account"
        )
    account_currency = config.account.currency
    if currency != account_currency:
        raise ValueError(
            f"Currency mismatch for symbol={symbol!r}: instrument={currency!r}, "
            f"account {config.account_id!r}={account_currency!r}"
        )
    security_type = route.get("security_type") or (registered.security_type if registered else None)
    exchange = route.get("exchange") or (registered.exchange if registered else None)
    calendar_id = route.get("calendar_id") or (registered.calendar_id if registered else None)
    execution_broker = route.get("broker") or config.broker
    if (data_adapter == "ibkr" or execution_broker == "ibkr") and not security_type:
        raise ValueError(
            f"No security_type for IBKR-routed symbol={symbol!r}; set "
            "instrument_overrides[symbol]['security_type']"
        )
    if security_type == "FUT" and not exchange:
        raise ValueError(
            f"No exchange for IBKR future={symbol!r}; set instrument_overrides[symbol]['exchange']"
        )
    continuous_alias_raw = route.get(
        "continuous_alias",
        registered.continuous_alias if registered else False,
    )
    if not isinstance(continuous_alias_raw, bool):
        raise TypeError(f"continuous_alias for symbol={symbol!r} must be a bool")
    contract_month_raw = route.get(
        "contract_month",
        registered.contract_month if registered else None,
    )
    contract_month = validate_contract_month(contract_month_raw)
    return SymbolInfo(
        symbol=symbol,
        market=market,
        data_source=data_source,
        instrument_type=instrument_type,
        multiplier=float(multiplier),
        data_adapter=data_adapter,
        venue_symbol=route.get("venue_symbol")
        or (registered.venue_symbol if registered else symbol),
        currency=currency,
        continuous_alias=continuous_alias_raw,
        contract_month=contract_month,
        tick_size=float(tick_size) if tick_size is not None else None,
        security_type=security_type,
        exchange=exchange,
        calendar_id=calendar_id,
    )
