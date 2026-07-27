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
belong in RunConfig.symbol_overrides; venue/data fields belong in
RunConfig.instrument_overrides. Execution brokerage is deliberately absent
from the registry and must be selected by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from librae.core.run_config import RunConfig

AdapterName = Literal["crypto", "ibkr", "shioaji"]
_ADAPTER_BY_DATA_SOURCE: dict[str, AdapterName] = {
    "binance_spot": "crypto",
    "binance_futures_continuous": "crypto",
    "ibkr": "ibkr",
    "shioaji": "shioaji",
}
_CURRENCY_BY_MARKET = {
    "crypto": "USDT",
    "tw_futures": "TWD",
    "us_equity": "USD",
}

# Contract expiry structure — orthogonal to continuous_alias (see the
# per-symbol entries below). 'spot' is the bare case (direct ownership, not
# an exchange-traded derivative); everything else is prefixed contract_* so
# the derivative family is filterable/greppable as a group, and so the
# multiplier-defaulting rule below (spot=1.0 automatic, contract_*
# explicit-required) can key off the prefix. Extend this set (and the
# matching DB CHECK constraint in db/timescale_init.sql) when a new type is
# actually needed — don't pre-enumerate speculative ones.
ALLOWED_INSTRUMENT_TYPES = frozenset(
    {
        "spot",
        "contract_perpetual",
        "contract_monthly",
        "contract_quarterly",
    }
)


@dataclass(frozen=True)
class SymbolInfo:
    """Registry entry for a single symbol.

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
    tick_size: float | None = None
    security_type: str | None = None
    exchange: str | None = None

    def __post_init__(self) -> None:
        if self.instrument_type not in ALLOWED_INSTRUMENT_TYPES:
            raise ValueError(
                f"{self.symbol!r} has instrument_type="
                f"{self.instrument_type!r}, not one of {sorted(ALLOWED_INSTRUMENT_TYPES)}"
            )
        if self.data_adapter not in _ADAPTER_BY_DATA_SOURCE.values():
            raise ValueError(f"{self.symbol!r} has unsupported data_adapter={self.data_adapter!r}")
        if self.multiplier <= 0:
            raise ValueError(f"{self.symbol!r} multiplier must be positive")
        if not self.venue_symbol:
            raise ValueError(f"{self.symbol!r} venue_symbol must be non-empty")
        if not self.currency:
            raise ValueError(f"{self.symbol!r} currency must be non-empty")


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
            continuous_alias=bool(data.get("continuous_alias", False)),
            tick_size=float(raw_tick_size) if raw_tick_size is not None else None,
            security_type=(
                str(data["security_type"]) if data.get("security_type") is not None else None
            ),
            exchange=str(data["exchange"]) if data.get("exchange") is not None else None,
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
            "continuous_alias": True,
            "multiplier": 200.0,  # 臺股期貨（大台）— required, no safe default for contract_* types
            "tick_size": 1.0,  # 1 個指數點；TXF/MXF/TMF 共用（已用 Shioaji 合約資料的 limit_up/down 驗證過）
        },
        "MXFR1": {
            "market": "tw_futures",
            "data_source": "shioaji",
            "instrument_type": "contract_monthly",
            "data_adapter": "shioaji",
            "currency": "TWD",
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
    cfg: RunConfig,
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
    route = (cfg.instrument_overrides or {}).get(symbol, {})
    costs = dict(cfg.cost_overrides or {})
    costs.update((cfg.symbol_overrides or {}).get(symbol, {}))

    market = route.get("market") or (registered.market if registered else cfg.market)
    data_source = route.get("data_source") or (
        registered.data_source if registered else cfg.data_source
    )
    data_adapter = route.get("data_adapter") or (
        registered.data_adapter if registered else _ADAPTER_BY_DATA_SOURCE.get(data_source)
    )
    if data_adapter not in _ADAPTER_BY_DATA_SOURCE.values():
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
            f"No multiplier for symbol={symbol!r}; set symbol_overrides[symbol]['multiplier']"
        )
    instrument_type = route.get("instrument_type") or (
        registered.instrument_type if registered else ("spot" if float(multiplier) == 1.0 else "")
    )
    tick_size = costs.get("tick_size", registered.tick_size if registered else None)
    currency = route.get("currency") or (
        registered.currency if registered else _CURRENCY_BY_MARKET.get(market, "")
    )
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
        continuous_alias=registered.continuous_alias if registered else False,
        tick_size=float(tick_size) if tick_size is not None else None,
        security_type=route.get("security_type")
        or (registered.security_type if registered else None),
        exchange=route.get("exchange") or (registered.exchange if registered else None),
    )
