"""Market-level configuration for backtesting.

Loads a single markets.yaml with one section per market type (crypto, tw_futures, etc.).
Each section contains cost parameters used to build CostModel.

Per-instrument details (symbol, min_qty, exchange) belong to the broker layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MarketConfig:
    """Market-level configuration for backtesting.

    Contains both market metadata and cost parameters.
    Used by CostModel.from_market() to build cost model.
    """

    name: str
    asset_class: str
    quote_currency: str
    timezone: str
    is_24h: bool
    commission_rate: float
    min_commission: float
    tax_rate: float
    slippage_ticks: int
    tick_size: float
    multiplier: float
    long_margin_rate: float
    short_margin_rate: float


def _default_markets_path() -> Path:
    """Return the default markets.yaml path."""
    return Path(__file__).resolve().parent / "markets.yaml"


def load_market_configs(path: str | Path | None = None) -> dict[str, MarketConfig]:
    """Load all market configs from markets.yaml.

    Returns:
        Dict mapping market name to MarketConfig.
    """
    yaml_path = Path(path) if path else _default_markets_path()

    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return {}

    markets: dict[str, MarketConfig] = {}
    for market_name, mdata in raw.items():
        if not isinstance(mdata, dict):
            continue
        markets[market_name] = MarketConfig(
            name=market_name,
            asset_class=str(mdata.get("asset_class", "unknown")),
            quote_currency=str(mdata.get("quote_currency", "USD")),
            timezone=str(mdata.get("timezone", "UTC")),
            is_24h=bool(mdata.get("is_24h", False)),
            commission_rate=float(mdata.get("commission_rate", 0.0)),
            min_commission=float(mdata.get("min_commission", 0.0)),
            tax_rate=float(mdata.get("tax_rate", 0.0)),
            slippage_ticks=int(mdata.get("slippage_ticks", 0)),
            tick_size=float(mdata.get("tick_size", 0.01)),
            multiplier=float(mdata.get("multiplier", 1.0)),
            long_margin_rate=float(mdata.get("long_margin_rate", 1.0)),
            short_margin_rate=float(mdata.get("short_margin_rate", 1.0)),
        )

    return markets


def get_market(market_name: str, path: str | Path | None = None) -> MarketConfig:
    """Get a single market config by name."""
    markets = load_market_configs(path)
    if market_name not in markets:
        available = list(markets.keys())
        raise KeyError(f"Market '{market_name}' not found. Available: {available}")
    return markets[market_name]
