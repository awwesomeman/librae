"""Two-layer market configuration: MarketConfig (asset-class) + InstrumentConfig (instrument).

Loads per-market YAML files from librae/config/markets/ directory.
Each file defines a market, defaults, and instruments that inherit defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class MarketConfig:
    """Asset-class level configuration."""

    asset_class: str
    quote_currency: str
    timezone: str
    is_24h: bool
    session_open: Optional[str] = None
    session_close: Optional[str] = None
    night_session_open: Optional[str] = None
    night_session_close: Optional[str] = None
    settlement_day: Optional[str] = None


@dataclass
class InstrumentConfig:
    """Individual instrument configuration.

    Fields inherit from market-level defaults, then override per instrument.
    """

    market_id: str
    exchange: str
    symbol: str
    tick_size: float
    tick_value: float
    trade_unit: float
    min_qty: float
    qty_precision: int
    margin_rate: float
    commission_rate: float
    min_commission: float
    transaction_tax: float
    slippage_ticks: int
    market: Optional[MarketConfig] = None

    @property
    def multiplier(self) -> float:
        """Contract multiplier: tick_value / tick_size."""
        return self.tick_value / self.tick_size if self.tick_size > 0 else 1.0


def _default_markets_dir() -> Path:
    """Return the default markets/ directory path."""
    return Path(__file__).resolve().parent / "markets"


def load_market_configs(path: str | Path | None = None) -> dict[str, InstrumentConfig]:
    """Load all market YAML files from a directory.

    Each file defines: market (asset-class config), defaults, instruments.
    Instrument fields inherit from defaults, then override.

    Args:
        path: Directory containing market YAML files.
              Defaults to librae/config/markets/.
    """
    markets_dir = Path(path) if path else _default_markets_dir()

    instruments: dict[str, InstrumentConfig] = {}

    for yaml_file in sorted(markets_dir.glob("*.yaml")):
        with open(yaml_file, "r") as f:
            raw = yaml.safe_load(f)

        if not raw:
            continue

        market_id = yaml_file.stem
        market_obj = _parse_market(raw.get("market", {}), market_id)
        defaults = raw.get("defaults", {})

        for inst_id, idata in raw.get("instruments", {}).items():
            merged = {**defaults, **(idata or {})}
            instruments[inst_id] = _build_instrument(inst_id, merged, market_id, market_obj)

    return instruments


def get_instrument(instrument_id: str, path: str | Path | None = None) -> InstrumentConfig:
    """Get a single instrument config by ID."""
    instruments = load_market_configs(path)
    if instrument_id not in instruments:
        available = list(instruments.keys())
        raise KeyError(f"Instrument '{instrument_id}' not found. Available: {available}")
    return instruments[instrument_id]


def _parse_market(mdata: dict, market_id: str) -> MarketConfig:
    return MarketConfig(
        asset_class=mdata.get("asset_class", "unknown"),
        quote_currency=mdata.get("quote_currency", "USD"),
        timezone=mdata.get("timezone", "UTC"),
        is_24h=mdata.get("is_24h", False),
        session_open=mdata.get("session_open"),
        session_close=mdata.get("session_close"),
        night_session_open=mdata.get("night_session_open"),
        night_session_close=mdata.get("night_session_close"),
        settlement_day=mdata.get("settlement_day"),
    )


def _build_instrument(
    inst_id: str,
    merged: dict,
    market_id: str,
    market_obj: MarketConfig,
) -> InstrumentConfig:
    return InstrumentConfig(
        market_id=market_id,
        exchange=str(merged.get("exchange", "")),
        symbol=str(merged["symbol"]),
        tick_size=float(merged.get("tick_size", 0.01)),
        tick_value=float(merged.get("tick_value", 0.01)),
        trade_unit=float(merged.get("trade_unit", 1)),
        min_qty=float(merged.get("min_qty", 1)),
        qty_precision=int(merged.get("qty_precision", 0)),
        margin_rate=float(merged.get("margin_rate", 0.0)),
        commission_rate=float(merged.get("commission_rate", 0.0)),
        min_commission=float(merged.get("min_commission", 0.0)),
        transaction_tax=float(merged.get("transaction_tax", 0.0)),
        slippage_ticks=int(merged.get("slippage_ticks", 0)),
        market=market_obj,
    )
