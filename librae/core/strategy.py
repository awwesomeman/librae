"""Strategy protocol and data types for the backtest engine.

Defines the contract between strategies and the engine:
- Strategy implements on_bar(ctx) → list[Action] | RebalanceTargets
- Engine provides Context with market data + portfolio state
- Engine executes Actions or portfolio targets via Executor
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal


@dataclass(frozen=True)
class Position:
    """Engine-owned position state, exposed to strategy via Context."""

    symbol: str
    side: Literal["long", "short"]
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
        symbols: All symbols in the dataset.
        bar: Current bar data as dict (OHLCV + features). Single-asset.
        bars: Current bar data per symbol. Multi-asset.
        positions: Open positions keyed by symbol.
        cash: Available cash.
        equity: Engine-calculated mark-to-market portfolio equity.
        period_index: 0-based index into the timeline.
    """

    ts: datetime
    symbol: str
    symbols: list[str]
    bar: dict[str, float]
    bars: dict[str, dict[str, float]]
    positions: dict[str, Position]
    cash: float
    equity: float
    period_index: int


@dataclass(frozen=True)
class Action:
    """What the strategy wants the engine to do.

    Attributes:
        quantity: None sizes using all available cash (single-asset default).
            When on_bar returns multiple long/short Actions for the same bar
            (e.g. a cross-sectional/stock-picking strategy opening several
            symbols at once), leaving quantity=None on more than one of them
            lets the first-processed Action consume all cash and starves the
            rest — set explicit per-symbol quantity (e.g. equal-weight sizing).
        fill_price: How to resolve the execution price on the *next* bar.
            str   — bar dict key (e.g. "open", "vwap"); uses that field's value.
            float — limit price; fills only if next bar's low <= price <= high.
            None  — use engine default (RunConfig.params["fill_price"], typically "open").
        stop_price: Absolute price that force-closes the position (stop-market
            order — fills at the worse of stop_price/bar-open on gap-through).
            Only applied on open/scale of a "long"/"short" action; the engine
            checks it every bar after this one until the position closes.
        take_profit_price: Absolute price that force-closes the position
            (limit order — fills exactly at this price when the bar's range
            touches it). Same lifecycle as stop_price.
    """

    type: Literal["long", "short", "close", "hold"]
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""
    fill_price: str | float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass(frozen=True)
class RebalanceTargets:
    """Portfolio-level target weights resolved by the engine on the next bar.

    Positive weights target long exposure and negative weights target short
    exposure. Symbols currently held but absent from ``weights`` target zero
    and are closed. Target quantities are calculated together at execution
    time from portfolio equity and each symbol's resolved fill price.

    Target weights need not sum to one; any remainder stays in cash.
    """

    weights: dict[str, float]
    fill_price: str | float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        for symbol, raw_weight in self.weights.items():
            if not symbol:
                raise ValueError("target weight symbols must be non-empty")
            if not isfinite(raw_weight):
                raise ValueError(f"target weight for {symbol!r} must be finite")


StrategyIntent = list[Action] | RebalanceTargets


@dataclass(frozen=True)
class Fill:
    """Execution result from an Executor."""

    symbol: str
    side: Literal["long", "short"]
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
    """

    symbol: str
    side: Literal["long", "short"]
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


class BaseStrategy(ABC):
    """Abstract base for all strategies.

    Strategies only do one thing: look at Context, return an execution intent.
    Data preparation (ETL, signals) is done externally before the backtest.
    """

    @abstractmethod
    def on_bar(self, ctx: Context) -> StrategyIntent:
        """Called once per bar. Return Actions, targets, or an empty list to hold."""
        ...
