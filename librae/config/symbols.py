"""Per-symbol registry — symbol -> market + data source + contract economics.

Market-level cost params (commission/tax/slippage/margin, plus an
approximate default tick_size — see markets.yaml) are genuinely shared
across every instrument in a market and live in markets.yaml (MarketConfig).

multiplier does NOT get a market-level fallback: for 'spot' instruments
it's a mathematical invariant (buying spot = 1 unit for 1 unit) and
defaults to 1.0 automatically here; for any contract_* instrument it
varies per contract (TXF=200 vs MXF=50 vs TMF=10, all "market: tw_futures")
and must be declared explicitly, no default — mirrors how mainstream
frameworks handle this (e.g. QuantConnect LEAN's
symbol-properties-database.csv: a market-wide wildcard row for equities,
an explicit row per specific futures contract).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Contract expiry structure — orthogonal to continuous_alias (see symbols.yaml
# module comment). 'spot' is the bare case (direct ownership, not an
# exchange-traded derivative); everything else is prefixed contract_* so the
# derivative family is filterable/greppable as a group, and so the
# multiplier-defaulting rule below (spot=1.0 automatic, contract_*
# explicit-required) can key off the prefix. Extend this set (and the
# matching DB CHECK constraint in deploy/timescale_init.sql) when a new
# type is actually needed — don't pre-enumerate speculative ones.
ALLOWED_INSTRUMENT_TYPES = frozenset({
    "spot",
    "contract_perpetual",
    "contract_monthly",
    "contract_quarterly",
})


@dataclass(frozen=True)
class SymbolInfo:
    """Registry entry for a single symbol.

    multiplier is always populated once loaded (never None) — either from
    an explicit symbols.yaml value, or auto-resolved to 1.0 for 'spot'
    (see load_symbol_registry). tick_size is optional; None means "use
    markets.yaml's market-level default" (an acceptable approximation —
    see markets.yaml's module docstring for why tick_size and multiplier
    have different risk profiles).
    """

    symbol: str
    market: str
    data_source: str
    instrument_type: str
    multiplier: float
    continuous_alias: bool = False
    tick_size: float | None = None

    def __post_init__(self) -> None:
        if self.instrument_type not in ALLOWED_INSTRUMENT_TYPES:
            raise ValueError(
                f"symbols.yaml: {self.symbol!r} has instrument_type="
                f"{self.instrument_type!r}, not one of {sorted(ALLOWED_INSTRUMENT_TYPES)}"
            )


def _default_symbols_path() -> Path:
    """Return the default symbols.yaml path."""
    return Path(__file__).resolve().parent / "symbols.yaml"


def load_symbol_registry(path: str | Path | None = None) -> dict[str, SymbolInfo]:
    """Load all symbol registry entries from symbols.yaml.

    Returns:
        Dict mapping symbol to SymbolInfo.

    Raises:
        ValueError: instrument_type is missing/invalid, or a contract_*
            instrument is missing 'multiplier' — caught here, at load
            time, rather than letting an unvalidated/defaulted value drift
            into a PnL calculation. 'spot' instruments don't need
            'multiplier' in the YAML at all (defaults to 1.0).
    """
    yaml_path = Path(path) if path else _default_symbols_path()

    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return {}

    registry: dict[str, SymbolInfo] = {}
    for symbol, data in raw.items():
        if not isinstance(data, dict):
            continue
        instrument_type = str(data.get("instrument_type", ""))
        raw_multiplier = data.get("multiplier")
        if raw_multiplier is not None:
            multiplier = float(raw_multiplier)
        elif instrument_type == "spot":
            multiplier = 1.0
        else:
            raise ValueError(
                f"symbols.yaml: {symbol!r} (instrument_type={instrument_type!r}) is "
                "missing 'multiplier' — only 'spot' gets a safe automatic default (1.0); "
                "contract_* instruments vary per contract and must declare it explicitly."
            )
        raw_tick_size = data.get("tick_size")
        registry[symbol] = SymbolInfo(
            symbol=symbol,
            market=str(data.get("market", "")),
            data_source=str(data.get("data_source", "")),
            instrument_type=instrument_type,
            multiplier=multiplier,
            continuous_alias=bool(data.get("continuous_alias", False)),
            tick_size=float(raw_tick_size) if raw_tick_size is not None else None,
        )
    return registry


def get_symbol(symbol: str, path: str | Path | None = None) -> SymbolInfo:
    """Get a single symbol's registry entry by name."""
    registry = load_symbol_registry(path)
    if symbol not in registry:
        available = list(registry.keys())
        raise KeyError(f"Symbol '{symbol}' not found. Available: {available}")
    return registry[symbol]
