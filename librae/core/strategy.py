"""Strategy protocol and data types for the backtest engine.

Defines the contract between strategies and the engine:
- Strategy implements on_bar(ctx) → list[Action]
- Engine provides Context with market data + portfolio state
- Engine executes Actions via Executor
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Position:
    """Engine-owned position state, exposed to strategy via Context."""

    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    quantity: float
    entry_ts: datetime
    periods_held: int
    unrealized_pnl: float


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
        bar_index: 0-based index into the timeline.
    """

    ts: datetime
    symbol: str
    symbols: list[str]
    bar: dict[str, float]
    bars: dict[str, dict[str, float]]
    positions: dict[str, Position]
    cash: float
    bar_index: int


@dataclass(frozen=True)
class Action:
    """What the strategy wants the engine to do."""

    type: Literal["buy", "sell", "close", "hold"]
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""


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
    entry_ts: datetime
    periods_held: int
    entry_commission: float
    entry_slippage: float
    total_entry_cost: float


class BaseStrategy(ABC):
    """Abstract base for all strategies.

    Strategies only do one thing: look at Context, return Actions.
    Data preparation (ETL, signals) is done externally before the backtest.
    """

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Action]:
        """Called once per bar. Return list of Actions (empty = hold)."""
        ...
