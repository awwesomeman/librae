"""Market-level configuration for backtesting.

Loads a single markets.yaml with one section per market type (crypto, tw_futures, etc.).
Each section contains cost parameters shared across every instrument traded
in that market — commission/tax/slippage/margin rate structure, plus
tick_size as a low-stakes approximate default (it only feeds a backtest
slippage-cost estimate, not PnL/margin sizing, so a market-wide value is an
acceptable default for large, homogeneous universes like equities/crypto
spot pairs — override per-symbol in symbols.yaml when precision matters).

multiplier is NOT here — mirrors mainstream frameworks (e.g. QuantConnect
LEAN's symbol-properties-database.csv, which uses a market-wide wildcard
row for equities but requires an explicit row per specific futures
contract): it directly scales PnL/notional/margin, and for a market with
heterogeneous contracts (e.g. tw_futures: TXF=200 vs MXF=50 vs TMF=10) a
single market-level number is actively wrong for most of them. See
librae/config/symbols.py — spot instruments default to 1.0 automatically
(a mathematical invariant, not a guess); contract_* instruments require it
explicit, no fallback.

Per-instrument details (symbol, min_qty, exchange) belong to the broker layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class MarketConfig:
    """Market-level configuration for backtesting.

    Used by CostModel.from_market() to build a cost model, along with the
    per-symbol multiplier (always) and tick_size (when overridden) from
    librae/config/symbols.py's SymbolInfo.
    """

    name: str
    commission_rate: float
    min_commission: float
    tax_rate: float
    slippage_ticks: int
    tick_size: float
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
            commission_rate=float(mdata.get("commission_rate", 0.0)),
            min_commission=float(mdata.get("min_commission", 0.0)),
            tax_rate=float(mdata.get("tax_rate", 0.0)),
            slippage_ticks=int(mdata.get("slippage_ticks", 0)),
            tick_size=float(mdata.get("tick_size", 0.01)),
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
