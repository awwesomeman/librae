"""Strategy protocol and data types for the backtest engine.

Defines the contract between strategies and the engine:
- Strategy implements on_bar(ctx) → list[OrderIntent] | PortfolioTargets
- Engine provides Context with market data + portfolio state
- Engine executes order intents or portfolio targets via the execution layer
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Literal

PositionSide = Literal["long", "short"]
OrderAction = Literal["long", "short", "close"]
PositionEventType = Literal["open", "add", "reduce", "close"]


@dataclass(frozen=True)
class Position:
    """Engine-owned position state, exposed to strategy via Context."""

    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float
    entry_at: datetime
    periods_held: int
    unrealized_pnl: float
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True, slots=True)
class Context:
    """Immutable snapshot passed to strategy on each bar.

    Attributes:
        ts: Current bar timestamp.
        symbol: Primary symbol (single-asset convenience).
        symbols: All configured symbols.
        bar: Current data for the primary symbol, or an empty dict when that
            symbol has no bar at this timestamp.
        bars: Current data keyed only by symbols with an observed bar at
            this timestamp. Last-known marks are not inserted here.
        positions: Open positions keyed by symbol.
        cash: Available cash.
        equity: Engine-calculated mark-to-market portfolio equity.
        period_index: 0-based strategy-callback count. Live arrival events can
            share a timestamp, so this is not a business-day index.
    """

    ts: datetime
    symbol: str
    symbols: tuple[str, ...]
    bar: Mapping[str, float]
    bars: Mapping[str, Mapping[str, float]]
    positions: Mapping[str, Position]
    cash: float
    equity: float
    period_index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", tuple(self.symbols))
        object.__setattr__(self, "bar", MappingProxyType(dict(self.bar)))
        object.__setattr__(
            self,
            "bars",
            MappingProxyType(
                {symbol: MappingProxyType(dict(values)) for symbol, values in self.bars.items()}
            ),
        )
        object.__setattr__(
            self,
            "positions",
            MappingProxyType(dict(self.positions)),
        )

    @property
    def available_symbols(self) -> tuple[str, ...]:
        """Symbols with a real, current bar in this event."""
        return tuple(self.bars)


@dataclass(frozen=True)
class OrderIntent:
    """A symbol-level instruction requested by a strategy.

    Attributes:
        quantity: None sizes using all available cash (single-asset default).
            When on_bar returns multiple long/short intents for the same bar
            (e.g. a cross-sectional/stock-picking strategy opening several
            symbols at once), leaving quantity=None on more than one of them
            lets the first-processed OrderIntent consume all cash and starves the
            rest — set explicit per-symbol quantity (e.g. equal-weight sizing).
        fill_price: In backtest/sim, how to resolve execution on the next bar.
            str   — bar dict key (e.g. "open", "vwap"); uses that field's value.
            float — one-bar limit order. Buys fill when low reaches the limit;
                sells fill when high reaches it. Gap-through fills at open.
            None  — use ``RunConfig.execution.default_fill_price``.
            In live, None submits a market order and a float submits a broker
            limit order; bar-field strings are rejected as non-causal.
        stop_price: Absolute price that force-closes the position (stop-market
            order — fills at the worse of stop_price/bar-open on gap-through).
            Only applied on open/scale of a "long"/"short" action; the engine
            checks it every bar after this one until the position closes.
            Simulation-only; live requires broker-native protective orders.
        take_profit_price: Absolute price that force-closes the position
            (limit order — fills exactly at this price when the bar's range
            touches it). Same lifecycle as stop_price and simulation-only.
    """

    action: OrderAction
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""
    fill_price: str | float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class PortfolioTargets:
    """Portfolio-level target weights.

    Positive weights target long exposure and negative weights target short
    exposure. Symbols currently held but absent from ``weights`` target zero
    and are closed. Backtest/sim resolves quantities on the next bar. Live
    sizes at the latest completed close and immediately submits market orders;
    live ``fill_price`` is therefore unsupported.

    Target weights need not sum to one; any remainder stays in cash.
    """

    weights: dict[str, float]
    fill_price: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "weights", dict(self.weights))
        if self.fill_price is not None and not isinstance(self.fill_price, str):
            raise ValueError(
                "PortfolioTargets.fill_price must be a bar field name; "
                "use per-symbol OrderIntent values for limit orders"
            )
        for symbol, raw_weight in self.weights.items():
            if not symbol:
                raise ValueError("target weight symbols must be non-empty")
            if not isfinite(raw_weight):
                raise ValueError(f"target weight for {symbol!r} must be finite")


StrategyDecision = list[OrderIntent] | PortfolioTargets


@dataclass(frozen=True)
class Fill:
    """Execution result from an Executor."""

    symbol: str
    side: PositionSide
    price: float
    quantity: float
    commission: float
    slippage: float
    tax: float


@dataclass
class PositionState:
    """Mutable internal position state — used by backtest + live engines.

    Not exposed to strategies (they see frozen Position via Context).
    Tracks accumulated entry-side costs for accurate PnL on close.

    On scaling: entry_price is derived from total_entry_cost / (quantity * multiplier).
    Storing total_entry_cost avoids float drift on repeated add operations.
    A volume-limited stop-market/liquidation keeps its pending exit reason so
    the remainder continues as a market exit on the next observed bar.
    """

    symbol: str
    side: PositionSide
    entry_price: float
    quantity: float
    entry_at: datetime
    periods_held: int
    entry_commission: float
    entry_slippage: float
    entry_tax: float
    total_entry_cost: float
    stop_price: float | None = None
    take_profit_price: float | None = None
    pending_market_exit_reason: str | None = None


class Strategy(ABC):
    """Abstract base for all strategies.

    Strategies only inspect Context and return a decision.
    Data preparation (ETL, signals) is done externally before the backtest.
    """

    @abstractmethod
    def on_bar(self, ctx: Context) -> StrategyDecision:
        """Return order intents, portfolio targets, or ``[]`` for no decision."""
        ...
