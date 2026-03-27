"""Two-layer market configuration: MarketConfig (asset-class) + InstrumentConfig (instrument).

Loads config/markets.yaml and resolves instrument -> market references.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import yaml
from pathlib import Path


@dataclass
class MarketConfig:
    asset_class: str          # crypto / futures / equity
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
    market_id: str
    exchange: str
    data_source: str
    base_asset: str
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
    default_timeframe: str
    warmup_bars: int
    max_hold_bars: int
    # resolved at load time
    market: Optional[MarketConfig] = None

    @property
    def commission_per_unit(self) -> float:
        """最低手續費保護：max(rate-based, min_commission)"""
        return self.min_commission

    @property
    def slippage_cost(self) -> float:
        """單邊滑點成本（以 quote_currency 計）"""
        return self.slippage_ticks * self.tick_value


def _default_yaml_path() -> Path:
    """Return the default config/markets.yaml path relative to project root."""
    return Path(__file__).resolve().parent / "markets.yaml"


def load_market_configs(path: str | Path | None = None) -> dict[str, InstrumentConfig]:
    """載入 config/markets.yaml，回傳 instrument_id -> InstrumentConfig dict"""
    yaml_path = Path(path) if path else _default_yaml_path()
    with open(yaml_path, "r") as f:
        raw = yaml.safe_load(f)

    # Parse markets
    markets: dict[str, MarketConfig] = {}
    for market_id, mdata in raw.get("markets", {}).items():
        markets[market_id] = MarketConfig(
            asset_class=mdata["asset_class"],
            quote_currency=mdata["quote_currency"],
            timezone=mdata["timezone"],
            is_24h=mdata["is_24h"],
            session_open=mdata.get("session_open"),
            session_close=mdata.get("session_close"),
            night_session_open=mdata.get("night_session_open"),
            night_session_close=mdata.get("night_session_close"),
            settlement_day=mdata.get("settlement_day"),
        )

    # Parse instruments and resolve market references
    instruments: dict[str, InstrumentConfig] = {}
    for inst_id, idata in raw.get("instruments", {}).items():
        market_ref = idata["market"]
        market_obj = markets.get(market_ref)
        instruments[inst_id] = InstrumentConfig(
            market_id=market_ref,
            exchange=idata["exchange"],
            data_source=idata["data_source"],
            base_asset=idata["base_asset"],
            symbol=idata["symbol"],
            tick_size=float(idata["tick_size"]),
            tick_value=float(idata["tick_value"]),
            trade_unit=float(idata["trade_unit"]),
            min_qty=float(idata["min_qty"]),
            qty_precision=int(idata["qty_precision"]),
            margin_rate=float(idata["margin_rate"]),
            commission_rate=float(idata["commission_rate"]),
            min_commission=float(idata["min_commission"]),
            transaction_tax=float(idata["transaction_tax"]),
            slippage_ticks=int(idata["slippage_ticks"]),
            default_timeframe=idata["default_timeframe"],
            warmup_bars=int(idata["warmup_bars"]),
            max_hold_bars=int(idata["max_hold_bars"]),
            market=market_obj,
        )

    return instruments


def get_instrument(instrument_id: str, path: str | Path | None = None) -> InstrumentConfig:
    """取得單一標的 config"""
    instruments = load_market_configs(path)
    if instrument_id not in instruments:
        available = list(instruments.keys())
        raise KeyError(f"Instrument '{instrument_id}' not found. Available: {available}")
    return instruments[instrument_id]


def calc_commission(inst: InstrumentConfig, price: float, qty: float) -> float:
    """計算手續費（考慮 min_commission）"""
    rate_based = price * qty * inst.commission_rate
    return max(rate_based, inst.min_commission)


def calc_slippage(inst: InstrumentConfig, qty: float) -> float:
    """計算滑點成本"""
    return inst.slippage_ticks * inst.tick_value * qty
