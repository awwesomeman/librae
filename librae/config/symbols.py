"""Per-symbol registry: symbol -> product, data route, and contract economics.

Market-level cost params (commission rate and floor, tax, slippage, margin,
plus an approximate default tick_size — see market_config.py) are genuinely
shared across every instrument in a market and live there (MarketConfig).

A product (a futures root such as TAIFEX's TX) holds what every contract of
it shares: market, instrument type, currency, calendar, multiplier, tick and
quantity rules, and the per-contract commission. A symbol names its product
and adds only its own route (data source, adapter, venue symbol, which
contract it selects), so a product's specs and fees are declared once. This
is how LEAN's symbol-properties-database.csv keys futures (one row per market
and root symbol, no expiry column) and how zipline's PerContract commission
takes a cost per root symbol.

multiplier does NOT get a market-level fallback: for 'spot' instruments
it's a mathematical invariant (buying spot = 1 unit for 1 unit) and
defaults to 1.0 automatically here; for any contract_* instrument it
varies per product (TX=200 vs MTX=50 vs TMF=10, all "market: tw_futures")
and must be declared explicitly, by the product or the symbol.

This registry is a plain Python dict rather than a bundled symbols.yaml:
the built wheel includes only .py files without extra packaging config, so
a data file would be missing after `pip install librae` and get_symbol()
would raise FileNotFoundError for any built-in symbol. A handful of
hardcoded entries needs no parser, no packaging config, and can't go
missing from the wheel.

Registering your own symbol doesn't require editing this file. A symbol of a
built-in product names it in RunConfig.instrument_overrides[symbol]['product']
and inherits its specs; other cost fields belong in
RunConfig.symbol_cost_overrides and venue/data fields in
RunConfig.instrument_overrides. Execution brokerage is deliberately absent
from the registry and must be selected by the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from math import isfinite
from numbers import Real
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from librae.core.run_config import RunConfig
    from librae.core.strategy import OrderIntent

from librae.core.utils import floor_to_step, validate_contract_month, validate_futures_selector

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
# This is the single source of truth for the value — code that persists an
# instrument type calls validate_instrument_type() below instead of
# re-declaring the set, so a caller who never touches TimescaleDB
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
_DATED_CONTRACT_TYPES = frozenset({"contract_monthly", "contract_quarterly"})
# The venue security types brokers here use, and the instrument types each
# can trade as. A type not listed is left to the adapter that consumes it.
_INSTRUMENT_TYPES_BY_SECURITY_TYPE: dict[str | None, frozenset[str]] = {
    "STK": frozenset({"spot"}),
    "FUT": _DATED_CONTRACT_TYPES,
}
INSTRUMENT_OVERRIDE_FIELDS = frozenset(
    {
        "broker",
        "calendar_id",
        "continuous_alias",
        "contract_month",
        "currency",
        "data_adapter",
        "data_source",
        "exchange",
        "instrument_type",
        "market",
        "min_notional",
        "min_quantity",
        "price_increment",
        "product",
        "quantity_step",
        "security_type",
        "venue_symbol",
    }
)
_INSTRUMENT_TEXT_FIELDS = frozenset(
    {
        "broker",
        "calendar_id",
        "currency",
        "data_adapter",
        "data_source",
        "exchange",
        "instrument_type",
        "market",
        "product",
        "security_type",
        "venue_symbol",
    }
)
_INSTRUMENT_POSITIVE_FIELDS = frozenset(
    {"min_notional", "min_quantity", "price_increment", "quantity_step"}
)
_PRICE_INCREMENT_TOLERANCE_RATE = Decimal("1e-9")


def canonicalize_price_to_increment(
    price: float,
    price_increment: float,
    *,
    context: str = "price",
) -> float:
    """Return the nearest authoritative grid price within representation tolerance."""
    if isinstance(price, bool) or not isinstance(price, Real) or not isfinite(price) or price <= 0:
        raise ValueError(f"{context} must be finite and positive")
    if (
        isinstance(price_increment, bool)
        or not isinstance(price_increment, Real)
        or not isfinite(price_increment)
        or price_increment <= 0
    ):
        raise ValueError("price_increment must be finite and positive")

    decimal_price = Decimal(str(price))
    increment = Decimal(str(price_increment))
    nearest = (decimal_price / increment).to_integral_value() * increment
    tolerance = increment * _PRICE_INCREMENT_TOLERANCE_RATE
    if abs(decimal_price - nearest) > tolerance:
        raise ValueError(
            f"{context}={price!r} is not aligned to price_increment={price_increment!r}"
        )
    return float(nearest)


def validate_instrument_type(instrument_type: str, *, context: str = "instrument_type") -> None:
    """Raise ValueError unless instrument_type is one of ALLOWED_INSTRUMENT_TYPES."""
    if instrument_type not in ALLOWED_INSTRUMENT_TYPES:
        raise ValueError(
            f"{context}={instrument_type!r} is not one of {sorted(ALLOWED_INSTRUMENT_TYPES)}"
        )


def validate_instrument_overrides(
    overrides: Mapping[str, object],
    *,
    symbols: tuple[str, ...],
) -> None:
    """Validate the closed per-symbol routing and execution schema."""
    for symbol, raw_route in overrides.items():
        if not isinstance(symbol, str):
            raise TypeError("instrument_overrides keys must be symbol strings")
        if symbol not in symbols:
            raise ValueError(f"instrument_overrides contains unconfigured symbol {symbol!r}")
        if not isinstance(raw_route, Mapping):
            raise TypeError(f"instrument_overrides[{symbol!r}] must be a mapping")
        route = dict(raw_route)
        unknown = sorted(
            repr(field_name)
            for field_name in route
            if not isinstance(field_name, str) or field_name not in INSTRUMENT_OVERRIDE_FIELDS
        )
        if unknown:
            raise ValueError(f"instrument_overrides[{symbol!r}] contains unknown keys {unknown}")
        for field_name in _INSTRUMENT_TEXT_FIELDS & set(route):
            value = route[field_name]
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"instrument_overrides[{symbol!r}][{field_name!r}] must be a non-empty string"
                )
        for field_name in _INSTRUMENT_POSITIVE_FIELDS & set(route):
            value = route[field_name]
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(
                    f"instrument_overrides[{symbol!r}][{field_name!r}] must be finite and positive"
                )
        if "continuous_alias" in route and not isinstance(route["continuous_alias"], bool):
            raise TypeError(f"instrument_overrides[{symbol!r}]['continuous_alias'] must be a bool")
        if "contract_month" in route:
            contract_month = route["contract_month"]
            if contract_month is not None and not isinstance(contract_month, str):
                raise TypeError(
                    f"instrument_overrides[{symbol!r}]['contract_month'] must be a string or None"
                )
            try:
                validate_contract_month(contract_month)
            except ValueError as exc:
                raise ValueError(
                    f"instrument_overrides[{symbol!r}]['contract_month']: {exc}"
                ) from exc
        if route.get("continuous_alias") is True and route.get("contract_month") is not None:
            raise ValueError(
                f"instrument_overrides[{symbol!r}] cannot set both "
                "continuous_alias=True and contract_month"
            )
        if "instrument_type" in route:
            validate_instrument_type(
                str(route["instrument_type"]),
                context=f"instrument_overrides[{symbol!r}]['instrument_type']",
            )
        if "product" in route:
            product_of(symbol, route)


def validate_data_source_routes(
    symbols: tuple[str, ...],
    *,
    data_source: str,
    overrides: Mapping[str, Mapping[str, object]],
) -> None:
    """Refuse a run whose data source contradicts a symbol's route.

    A registered symbol takes its ``data_source``, ``instrument_type`` and
    ``venue_symbol`` from the registry unless overridden, and the last two
    pick the market orders go to. Changing some of them while inheriting the
    rest would read, say, spot bars while trading the perpetual, so an
    override that changes any of the three must state all three. Its
    ``data_adapter`` fetches the bars that ``data_source`` labels, so changing
    the adapter must state all three too.
    ``data_source`` names the run's one source, or ``"multi"`` for a mixed
    run, so it must match what every symbol resolves to; ``"multi"`` names no
    source, so under it an unregistered symbol needs its own, and no symbol
    may take ``"multi"`` as its own.
    """
    for symbol in symbols:
        registered = _BUILTIN_SYMBOLS.get(symbol)
        route = overrides.get(symbol, {})
        override_source = route.get("data_source")
        if override_source == "multi":
            raise ValueError(
                f"instrument_overrides[{symbol!r}]['data_source']='multi' names no source; "
                "set the source this symbol's bars come from"
            )
        if registered is not None:
            _check_registered_route(symbol, registered, route)
        elif override_source is None and data_source == "multi":
            raise ValueError(
                f"{symbol!r} is not registered and has no "
                f"instrument_overrides[{symbol!r}]['data_source']; data_source='multi' "
                "marks a run mixing sources and names none, so set the symbol's there"
            )
        if data_source == "multi":
            continue
        resolved = override_source or (registered.data_source if registered else data_source)
        if resolved != data_source:
            origin = "instrument_overrides" if override_source else "the built-in registry"
            hint = (
                ""
                if override_source
                else "; to read a registered symbol elsewhere, override its "
                "data_source, instrument_type and venue_symbol"
            )
            raise ValueError(
                f"data_source={data_source!r} disagrees with {symbol!r}, which {origin} "
                f"routes to {resolved!r}; declare the matching data_source, or 'multi' "
                f"for a run mixing sources{hint}"
            )


_ROUTE_FIELDS = ("data_source", "instrument_type", "venue_symbol")
# Changing the adapter needs the full route, or its bars would carry the
# registered source's label. A route need not restate the adapter: one
# adapter can serve several sources, as the crypto one does spot and
# perpetual bars.
_ROUTE_CHANGING_FIELDS = (*_ROUTE_FIELDS, "data_adapter")


def _check_registered_route(
    symbol: str,
    registered: SymbolInfo,
    route: Mapping[str, object],
) -> None:
    changed = [
        name
        for name in _ROUTE_CHANGING_FIELDS
        if name in route and route[name] != getattr(registered, name)
    ]
    missing = [name for name in _ROUTE_FIELDS if name not in route]
    if changed and missing:
        moved = ", ".join(f"{name}={route[name]!r}" for name in changed)
        kept = ", ".join(f"{name}={getattr(registered, name)!r}" for name in missing)
        raise ValueError(
            f"instrument_overrides[{symbol!r}] changes registered symbol {symbol!r} to "
            f"{moved} but inherits its registered {kept}; state all of data_source, "
            "instrument_type and venue_symbol there, restating a registered value "
            "where it still holds, since the source labels the bars the adapter "
            "fetches and the other two pick where orders go"
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
    price_increment: float | None = None
    min_notional: float | None = None

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
        for field_name in ("multiplier", "tick_size", "price_increment", "min_notional"):
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
        for key in ("price_increment", "min_notional"):
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
    ``product`` names the ProductInfo the contract specs were taken from, or
    None for a symbol that declares its own.

    multiplier is always populated once loaded (never None) — either from
    an explicit value, or auto-resolved to 1.0 for 'spot' (see
    _build_registry). tick_size is optional; None means "use
    market_config.py's market-level default" (an acceptable approximation
    — see this module's docstring for why tick_size and multiplier have
    different risk profiles).
    ``price_increment`` is separate, authoritative order metadata and is
    never inferred from the cost model. Quantity step, minimum quantity,
    and minimum notional are optional shared execution
    constraints; venue rules that must be discovered at order time remain
    adapter-owned.
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
    quantity_step: float | None = None
    min_quantity: float | None = None
    price_increment: float | None = None
    min_notional: float | None = None
    product: str | None = None

    def __post_init__(self) -> None:
        validate_instrument_type(self.instrument_type, context=f"{self.symbol!r} instrument_type")
        if not isinstance(self.data_adapter, str) or not self.data_adapter:
            raise ValueError(f"{self.symbol!r} data_adapter must be a non-empty string")
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, Real)
            or not isfinite(self.multiplier)
            or self.multiplier <= 0
        ):
            raise ValueError(f"{self.symbol!r} multiplier must be finite and positive")
        if not self.venue_symbol:
            raise ValueError(f"{self.symbol!r} venue_symbol must be non-empty")
        if not self.currency:
            raise ValueError(f"{self.symbol!r} currency must be non-empty")
        if self.calendar_id is not None and not self.calendar_id:
            raise ValueError(f"{self.symbol!r} calendar_id must be non-empty or None")
        for field_name in (
            "tick_size",
            "quantity_step",
            "min_quantity",
            "price_increment",
            "min_notional",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{self.symbol!r} {field_name} must be positive when supplied")
        if not isinstance(self.continuous_alias, bool):
            raise TypeError(f"{self.symbol!r} continuous_alias must be a bool")
        validate_contract_month(self.contract_month)
        dated_contract = self.instrument_type in _DATED_CONTRACT_TYPES
        # security_type picks the venue contract that bars and orders use,
        # while instrument_type drives accounting, shorting and margin, so
        # the two must name the same product.
        allowed = _INSTRUMENT_TYPES_BY_SECURITY_TYPE.get(self.security_type)
        if allowed is not None and self.instrument_type not in allowed:
            raise ValueError(
                f"{self.symbol!r} security_type={self.security_type!r} contradicts "
                f"instrument_type={self.instrument_type!r}; it trades as one of "
                f"{sorted(allowed)}, so set both to the same product; an override "
                "cannot clear a registered security_type, so to trade a registered "
                "symbol as another product, key that product under its own symbol"
            )
        if dated_contract:
            validate_futures_selector(
                self.continuous_alias,
                self.contract_month,
                context=f"{self.symbol!r} future",
                alias_meaning="the venue's rolling contract alias",
            )
        if not dated_contract and self.contract_month is not None:
            raise ValueError(
                f"{self.symbol!r} contract_month is valid only for monthly/quarterly contracts"
            )

    @property
    def can_short(self) -> bool:
        """Whether a short may be opened or added.

        Crypto spot orders sell owned inventory with no borrow, which is why
        CryptoAdapter.prepare_order refuses a spot short open. Equity spot can
        be sold short on margin, and contracts short natively. A config's
        ``market`` is a declaration for every unregistered symbol that does
        not override it, so one declared spot in a crypto run is refused.
        """
        return not (self.market == "crypto" and self.instrument_type == "spot")

    def normalize_quantity(self, quantity: float) -> float:
        """Round a requested quantity toward zero and enforce its minimum."""
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, Real)
            or not isfinite(quantity)
            or quantity <= 0
        ):
            raise ValueError(f"{self.symbol!r} quantity must be positive and finite")
        normalized = (
            floor_to_step(float(quantity), self.quantity_step)
            if self.quantity_step is not None
            else float(quantity)
        )
        if self.min_quantity is not None and normalized < self.min_quantity:
            return 0.0
        return normalized

    def validate_order_prices(self, intent: OrderIntent) -> None:
        """Reject explicit order prices outside the authoritative grid."""
        if self.price_increment is None:
            return
        for field_name in ("limit_price", "stop_price", "take_profit_price"):
            raw_price = getattr(intent, field_name)
            if raw_price is None:
                continue
            canonicalize_price_to_increment(
                raw_price,
                self.price_increment,
                context=f"{self.symbol!r} {field_name}",
            )


@dataclass(frozen=True)
class ProductInfo:
    """Contract specs and fees every symbol of one product shares.

    A product is a futures root (TAIFEX's TX, MTX, TMF): its contracts, and
    the continuous series made of them, differ only in which contract they
    select. ``commission_per_contract`` is the per-side broker fee per
    contract.
    """

    product: str
    market: str
    instrument_type: str
    currency: str
    multiplier: float
    commission_per_contract: float
    calendar_id: str | None = None
    tick_size: float | None = None
    price_increment: float | None = None
    quantity_step: float | None = None
    min_quantity: float | None = None

    def __post_init__(self) -> None:
        validate_instrument_type(
            self.instrument_type, context=f"product {self.product!r} instrument_type"
        )
        for field_name in (
            "multiplier",
            "tick_size",
            "price_increment",
            "quantity_step",
            "min_quantity",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"product {self.product!r} {field_name} must be positive")
        fee = self.commission_per_contract
        if isinstance(fee, bool) or not isinstance(fee, Real) or not isfinite(fee) or fee < 0:
            raise ValueError(
                f"product {self.product!r} commission_per_contract must be finite and >= 0"
            )


# What a symbol of a product inherits from it. A registry entry naming a
# product may not restate one, so each value has one home.
_PRODUCT_SPEC_FIELDS = (
    "market",
    "instrument_type",
    "currency",
    "calendar_id",
    "multiplier",
    "tick_size",
    "price_increment",
    "quantity_step",
    "min_quantity",
)


def _taifex_index_future(product: str, multiplier: float, fee: float) -> ProductInfo:
    return ProductInfo(
        product=product,
        market="tw_futures",
        instrument_type="contract_monthly",
        currency="TWD",
        calendar_id="XTAIFEX",
        multiplier=multiplier,
        commission_per_contract=fee,
        tick_size=1.0,  # 1 index point; venue price limits remain contract-specific
        price_increment=1.0,
        quantity_step=1.0,
        min_quantity=1.0,
    )


# Keyed by the exchange's product code, not a broker's symbol. Multipliers are
# TAIFEX's contract specs (index x 200 / 50 / 10 TWD). Fees are the user's
# broker rate (大台 40 / 小台 30 / 微台 20 TWD per side), not an exchange rule:
# TAIFEX does not set broker commission.
_BUILTIN_PRODUCTS: dict[str, ProductInfo] = {
    "TX": _taifex_index_future("TX", 200.0, 40.0),
    "MTX": _taifex_index_future("MTX", 50.0, 30.0),
    "TMF": _taifex_index_future("TMF", 10.0, 20.0),
}


def get_product(product: str) -> ProductInfo:
    """Get a built-in product's specs by its code."""
    if product not in _BUILTIN_PRODUCTS:
        raise KeyError(f"Product {product!r} not found. Available: {sorted(_BUILTIN_PRODUCTS)}")
    return _BUILTIN_PRODUCTS[product]


def product_of(symbol: str, route: Mapping[str, object]) -> ProductInfo | None:
    """The product ``symbol`` belongs to: its override's, else its registered one.

    A registered symbol's product is part of its identity, so an override may
    restate it but not change it; key another product's contract under its
    own symbol instead.
    """
    registered = _BUILTIN_SYMBOLS.get(symbol)
    registered_product = registered.product if registered is not None else None
    name = route.get("product", registered_product)
    if name is None:
        return None
    if registered is not None and name != registered_product:
        raise ValueError(
            f"instrument_overrides[{symbol!r}]['product']={name!r} contradicts registered "
            f"symbol {symbol!r}, whose product is {registered_product!r}; key a contract "
            "of another product under its own symbol"
        )
    if not isinstance(name, str) or name not in _BUILTIN_PRODUCTS:
        raise ValueError(
            f"instrument_overrides[{symbol!r}]['product']={name!r} is not a known product; "
            f"one of {sorted(_BUILTIN_PRODUCTS)}, or leave it out and set the symbol's "
            "specs in instrument_overrides and symbol_cost_overrides"
        )
    return _BUILTIN_PRODUCTS[name]


def declared_specs(symbol: str, route: Mapping[str, object]) -> SymbolInfo | ProductInfo | None:
    """Where ``symbol``'s specs come from before overrides: its registry entry,
    else the product its route names, else nowhere (None).

    Both types carry every _PRODUCT_SPEC_FIELDS attribute.
    """
    registered = _BUILTIN_SYMBOLS.get(symbol)
    if registered is not None:
        product_of(symbol, route)
        return registered
    return product_of(symbol, route)


# The product specs a cost override would set; the rest come from routes.
_PRODUCT_COST_FIELDS = frozenset({"multiplier", "tick_size"})


def validate_product_overrides(
    symbols: tuple[str, ...],
    *,
    instrument_overrides: Mapping[str, Mapping[str, object]],
    cost_overrides: Mapping[str, object],
    symbol_cost_overrides: Mapping[str, Mapping[str, object]],
) -> None:
    """Refuse an override that changes a spec of the symbol's product.

    A product's specs and fee belong together: a multiplier changed on one
    symbol while it keeps the product's fee describes no real contract. A
    broker fee may differ from the product's, so commission_per_contract
    stays overridable.
    """
    for symbol in symbols:
        route = instrument_overrides.get(symbol, {})
        product = product_of(symbol, route)
        if product is None:
            continue
        costs = {**cost_overrides, **symbol_cost_overrides.get(symbol, {})}
        for name in _PRODUCT_SPEC_FIELDS:
            where = "cost overrides" if name in _PRODUCT_COST_FIELDS else "instrument_overrides"
            values = costs if name in _PRODUCT_COST_FIELDS else route
            if name not in values or values[name] == getattr(product, name):
                continue
            raise ValueError(
                f"{where} set {name}={values[name]!r} for {symbol!r}, whose product "
                f"{product.product!r} has {name}={getattr(product, name)!r}; a contract with "
                "other specs is not of that product, so key it under its own symbol without "
                "'product' and declare its specs there"
            )


def default_commission_per_contract(symbol: str, market: str, product: ProductInfo | None) -> float:
    """Per-contract commission for a symbol that does not set its own.

    A symbol pays its product's fee. A tw_futures symbol with no product
    raises instead of paying 0, because a silent 0 understates every backtest
    and pre-trade cost estimate. Other markets charge by rate and floor only
    (market_config.py).
    """
    if product is not None:
        return product.commission_per_contract
    if market != "tw_futures":
        return 0.0
    raise ValueError(
        f"No per-contract commission for tw_futures symbol={symbol!r}: name its product "
        f"in instrument_overrides[symbol]['product'] (one of {sorted(_BUILTIN_PRODUCTS)}) "
        "or set symbol_cost_overrides[symbol]['commission_per_contract']"
    )


_REGISTRY_FIELDS = frozenset(
    {
        "calendar_id",
        "continuous_alias",
        "contract_month",
        "currency",
        "data_adapter",
        "data_source",
        "exchange",
        "instrument_type",
        "market",
        "min_notional",
        "min_quantity",
        "multiplier",
        "price_increment",
        "product",
        "quantity_step",
        "security_type",
        "tick_size",
        "venue_symbol",
    }
)


def _build_registry(raw: dict[str, dict]) -> dict[str, SymbolInfo]:
    """Validate + construct a symbol registry from a plain raw dict.

    Raises:
        ValueError: an entry has a key this function does not read (a
            misspelling, or a field such as commission_per_contract that
            comes from elsewhere, would otherwise have no effect);
            instrument_type is missing/invalid, or a contract_*
            instrument is missing 'multiplier' — caught here, at build
            time, rather than letting an unvalidated/defaulted value drift
            into a PnL calculation. 'spot' instruments don't need
            'multiplier' at all (defaults to 1.0). An entry naming a product
            restates one of the product's specs, or names an unknown product.
    """
    registry: dict[str, SymbolInfo] = {}
    for symbol, data in raw.items():
        unknown = sorted(set(data) - _REGISTRY_FIELDS)
        if unknown:
            raise ValueError(f"registry entry {symbol!r} has unknown keys {unknown}")
        if "product" in data:
            product = _BUILTIN_PRODUCTS.get(data["product"])
            if product is None:
                raise ValueError(
                    f"registry entry {symbol!r} names unknown product {data['product']!r}"
                )
            restated = sorted(set(data) & set(_PRODUCT_SPEC_FIELDS))
            if restated:
                raise ValueError(
                    f"registry entry {symbol!r} restates {restated}, which its product "
                    f"{product.product!r} sets"
                )
            inherited = {name: getattr(product, name) for name in _PRODUCT_SPEC_FIELDS}
            data = {**{k: v for k, v in inherited.items() if v is not None}, **data}
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
            quantity_step=(
                float(data["quantity_step"]) if data.get("quantity_step") is not None else None
            ),
            min_quantity=(
                float(data["min_quantity"]) if data.get("min_quantity") is not None else None
            ),
            price_increment=(
                float(data["price_increment"]) if data.get("price_increment") is not None else None
            ),
            min_notional=(
                float(data["min_notional"]) if data.get("min_notional") is not None else None
            ),
            product=data.get("product"),
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
            "product": "TX",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,  # Shioaji native alias, rank 0 (nearest); see shioaji_adapter.py
        },
        "TXFR2": {
            "product": "TX",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,  # rank 1 (next-nearest)
        },
        "MXFR1": {
            "product": "MTX",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,
        },
        "MXFR2": {
            "product": "MTX",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,
        },
        "TMFR1": {
            "product": "TMF",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,
        },
        "TMFR2": {
            "product": "TMF",
            "data_source": "shioaji",
            "data_adapter": "shioaji",
            "continuous_alias": True,
        },
        "MU": {
            "market": "us_equity",
            "data_source": "ibkr",
            "instrument_type": "spot",
            "data_adapter": "ibkr",
            "currency": "USD",
            "security_type": "STK",
            "calendar_id": "XNYS",
            "quantity_step": 1.0,
            "min_quantity": 1.0,
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


def resolve_symbol(
    config: RunConfig,
    symbol: str,
    *,
    multiplier: float | None = None,
) -> SymbolInfo:
    """Resolve accounting and broker metadata for one configured symbol.

    Registry values are authoritative for registered symbols, and an
    unregistered symbol naming a product takes that product's specs. Run-wide
    market/data_source/calendar_id values are fallbacks for homogeneous,
    unregistered universes; ``instrument_overrides`` supplies per-symbol
    routing metadata.
    """
    registered = _BUILTIN_SYMBOLS.get(symbol)
    route = (config.instrument_overrides or {}).get(symbol, {})
    specs = declared_specs(symbol, route)
    costs = dict(config.cost_overrides or {})
    costs.update((config.symbol_cost_overrides or {}).get(symbol, {}))

    market = route.get("market") or (specs.market if specs else config.market)
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
        else costs.get("multiplier", specs.multiplier if specs else None)
    )
    if multiplier is None:
        raise ValueError(
            f"No multiplier for symbol={symbol!r}; set symbol_cost_overrides[symbol]['multiplier']"
        )
    instrument_type = route.get("instrument_type") or (specs.instrument_type if specs else "")
    if not instrument_type:
        raise ValueError(
            f"No instrument_type for symbol={symbol!r}; set "
            "instrument_overrides[symbol]['instrument_type']"
        )
    tick_size = costs.get("tick_size", specs.tick_size if specs else None)
    currency = route.get("currency") or (specs.currency if specs else "")
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
    calendar_id = route.get("calendar_id") or (specs.calendar_id if specs else config.calendar_id)
    quantity_step = route.get("quantity_step", specs.quantity_step if specs else None)
    min_quantity = route.get("min_quantity", specs.min_quantity if specs else None)
    price_increment = route.get("price_increment", specs.price_increment if specs else None)
    min_notional = route.get(
        "min_notional",
        registered.min_notional if registered else None,
    )
    execution_broker = config.broker_for(symbol)
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
        quantity_step=float(quantity_step) if quantity_step is not None else None,
        min_quantity=float(min_quantity) if min_quantity is not None else None,
        price_increment=(float(price_increment) if price_increment is not None else None),
        min_notional=float(min_notional) if min_notional is not None else None,
        product=route.get("product") or (registered.product if registered else None),
    )
