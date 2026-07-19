"""Per-symbol registry — symbol -> market + data source mapping.

Market-level cost/contract params live in markets.yaml (MarketConfig).
This is the thin layer above it: which market a symbol belongs to, and its
default data source.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Contract expiry structure — orthogonal to continuous_alias (see symbols.yaml
# module comment). 'spot' is the bare case (direct ownership, not an
# exchange-traded derivative); everything else is prefixed contract_* so the
# derivative family is filterable/greppable as a group. Extend this set (and
# the matching DB CHECK constraint in deploy/timescale_init.sql) when a new
# type is actually needed — don't pre-enumerate speculative ones.
ALLOWED_INSTRUMENT_TYPES = frozenset({
    "spot",
    "contract_perpetual",
    "contract_monthly",
    "contract_quarterly",
})


@dataclass(frozen=True)
class SymbolInfo:
    """Registry entry for a single symbol."""

    symbol: str
    market: str
    data_source: str
    instrument_type: str
    continuous_alias: bool = False

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
        ValueError: If an entry's instrument_type is missing or not in
            ALLOWED_INSTRUMENT_TYPES — caught here, at load time, rather
            than letting an unvalidated string drift into the DB.
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
        registry[symbol] = SymbolInfo(
            symbol=symbol,
            market=str(data.get("market", "")),
            data_source=str(data.get("data_source", "")),
            instrument_type=str(data.get("instrument_type", "")),
            continuous_alias=bool(data.get("continuous_alias", False)),
        )
    return registry


def get_symbol(symbol: str, path: str | Path | None = None) -> SymbolInfo:
    """Get a single symbol's registry entry by name."""
    registry = load_symbol_registry(path)
    if symbol not in registry:
        available = list(registry.keys())
        raise KeyError(f"Symbol '{symbol}' not found. Available: {available}")
    return registry[symbol]
