"""Strategy protocol and data types for the backtest engine.

Defines the contract between strategies and the engine:
- Strategy implements on_bar(ctx) → list[OrderIntent] | PortfolioTargets | MultiLegOrder
- Engine provides Context with market data + portfolio state
- Engine executes symbol intents, portfolio targets, or a multi-leg group
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from numbers import Real
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
            (limit order — fills at the target when touched, or at the better
            bar open after a favorable gap). Same lifecycle as stop_price and
            simulation-only.
            For a new entry whose simulated fill time within the bar is
            ambiguous (for example, a resting limit), protection starts on the
            next bar. Entries known to fill at open may trigger it immediately.
    """

    action: OrderAction
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""
    fill_price: str | float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None

    def __post_init__(self) -> None:
        if self.action not in ("long", "short", "close"):
            raise ValueError(f"invalid order action: {self.action!r}")
        if not isinstance(self.symbol, str):
            raise TypeError("OrderIntent.symbol must be a string")
        if not isinstance(self.reason, str):
            raise TypeError("OrderIntent.reason must be a string")

        if self.quantity is not None:
            _validate_positive_finite_number(self.quantity, "OrderIntent.quantity")

        fill_price = self.fill_price
        if isinstance(fill_price, str):
            if not fill_price:
                raise ValueError("OrderIntent.fill_price must be a non-empty bar field name")
        elif fill_price is not None:
            _validate_positive_finite_number(fill_price, "OrderIntent.fill_price")

        for field_name in ("stop_price", "take_profit_price"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_positive_finite_number(value, f"OrderIntent.{field_name}")
        if self.action == "close" and (
            self.stop_price is not None or self.take_profit_price is not None
        ):
            raise ValueError("close intents cannot set stop_price or take_profit_price")


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

    weights: Mapping[str, float]
    fill_price: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if self.fill_price is not None and (
            not isinstance(self.fill_price, str) or not self.fill_price
        ):
            raise ValueError(
                "PortfolioTargets.fill_price must be a non-empty bar field name; "
                "use per-symbol OrderIntent values for limit orders"
            )
        if not isinstance(self.reason, str):
            raise TypeError("PortfolioTargets.reason must be a string")

        weights = dict(self.weights)
        for symbol, raw_weight in weights.items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("target weight symbols must be non-empty")
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, Real):
                raise TypeError(f"target weight for {symbol!r} must be numeric")
            if not isfinite(raw_weight):
                raise ValueError(f"target weight for {symbol!r} must be finite")
        object.__setattr__(self, "weights", MappingProxyType(weights))

    def __deepcopy__(self, memo: dict[int, object]) -> PortfolioTargets:
        """Immutable value objects can be shared across runtime snapshots."""
        return self


@dataclass(frozen=True)
class MultiLegOrder:
    """One best-effort hedge group executed in declared leg order.

    This contract is intended for spreads across instruments or venues where
    no atomic exchange-native combo order exists. Every leg must be explicitly
    sized. Backtest/sim uses one synchronous market-data event; live submits
    one leg at a time and unwinds filled legs if the group cannot complete
    within ``max_unhedged_seconds`` or a leg fails.
    """

    legs: tuple[OrderIntent, ...]
    max_unhedged_seconds: float = 5.0
    reason: str = ""

    def __post_init__(self) -> None:
        legs = tuple(self.legs)
        if len(legs) < 2:
            raise ValueError("MultiLegOrder requires at least two legs")
        if any(not isinstance(leg, OrderIntent) for leg in legs):
            raise TypeError("MultiLegOrder.legs must contain only OrderIntent values")
        if any(not leg.symbol for leg in legs):
            raise ValueError("MultiLegOrder legs require explicit symbols")
        if any(leg.quantity is None for leg in legs):
            raise ValueError("MultiLegOrder legs require explicit quantities")
        symbols = [leg.symbol for leg in legs]
        if len(symbols) != len(set(symbols)):
            raise ValueError("MultiLegOrder requires at most one leg per symbol")
        _validate_positive_finite_number(
            self.max_unhedged_seconds,
            "MultiLegOrder.max_unhedged_seconds",
        )
        if not isinstance(self.reason, str):
            raise TypeError("MultiLegOrder.reason must be a string")
        object.__setattr__(self, "legs", legs)


StrategyDecision = list[OrderIntent] | PortfolioTargets | MultiLegOrder


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
        """Return intents, targets, a multi-leg order, or ``[]``."""
        ...


def _validate_positive_finite_number(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
