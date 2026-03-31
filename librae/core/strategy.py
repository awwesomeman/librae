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

    instrument: str
    side: Literal["long", "short"]
    entry_price: float
    quantity: float
    entry_ts: datetime
    bars_held: int
    unrealized_pnl: float


@dataclass(frozen=True)
class Context:
    """Immutable snapshot passed to strategy on each bar.

    Attributes:
        ts: Current bar timestamp.
        instrument: Primary instrument (single-asset convenience).
        instruments: All instruments in the dataset.
        bar: Current bar data as dict (OHLCV + features). Single-asset.
        bars: Current bar data per instrument. Multi-asset.
        positions: Open positions keyed by instrument.
        cash: Available cash.
        bar_index: 0-based index into the timeline.
    """

    ts: datetime
    instrument: str
    instruments: list[str]
    bar: dict[str, float]
    bars: dict[str, dict[str, float]]
    positions: dict[str, Position]
    cash: float
    bar_index: int


@dataclass(frozen=True)
class Action:
    """What the strategy wants the engine to do."""

    type: Literal["buy", "sell", "close", "hold"]
    instrument: str = ""
    quantity: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class Fill:
    """Execution result from an Executor."""

    instrument: str
    side: Literal["long", "short"]
    price: float
    quantity: float
    commission: float
    slippage: float
    tax: float


class BaseStrategy(ABC):
    """Abstract base for all strategies.

    Strategies only do one thing: look at Context, return Actions.
    Data preparation (ETL, signals) is done externally before the backtest.
    """

    @abstractmethod
    def on_bar(self, ctx: Context) -> list[Action]:
        """Called once per bar. Return list of Actions (empty = hold)."""
        ...
