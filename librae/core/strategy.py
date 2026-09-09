"""Strategy protocol and data types for the backtest engine.

Defines the contract between strategies and the engine:
- Strategy implements on_bar(ctx) → list[OrderIntent] | PortfolioWeights
- Engine provides Context with market data + portfolio state
- Engine executes symbol intents or portfolio weights. A non-None group_id
  identifies related legs, but its guarantee is mode-specific: backtest/sim
  stage the group as one local fill-or-kill unit, while live preflights it
  before serial broker requests and cannot guarantee venue atomicity.
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

import pandas as pd

from librae.core.market_data import MarketDataView

PositionSide = Literal["long", "short"]
OrderAction = Literal["long", "short", "close"]
PositionEventType = Literal["open", "add", "reduce", "close"]
TimeInForce = Literal["day", "gtc", "ioc", "fok"]
# Values whose lifetime spans more than one execution event. Simulation keeps
# these on the engine's pending decision until they fill or expire; every other
# value resolves on its first eligible event.
RESTING_TIME_IN_FORCE: tuple[TimeInForce, ...] = ("day", "gtc")


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
class AccountSnapshot:
    """Strategy-visible snapshot of one isolated account ledger."""

    currency: str
    cash: float
    equity: float


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
        account_id: Stable identity of this run's account.
        account: Cash and mark-to-market equity for this run's account.
        period_index: 0-based count of committed strategy callbacks. Live
            arrival events can share a timestamp, so this is not a
            business-day index, and a retried event repeats its index rather
            than advancing it.
        decision_at: Point-in-time information frontier for this callback.
            ``None`` preserves the legacy single-frame contract.
        market_data: Immutable, exact-subscription as-of history. ``None``
            preserves the legacy single-frame contract.
    """

    ts: datetime
    symbol: str
    symbols: tuple[str, ...]
    bar: Mapping[str, float]
    bars: Mapping[str, Mapping[str, float]]
    positions: Mapping[str, Position]
    account_id: str
    account: AccountSnapshot
    period_index: int
    decision_at: datetime | None = None
    market_data: MarketDataView | None = None

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
        if not isinstance(self.account_id, str) or not self.account_id:
            raise ValueError("account_id must be a non-empty string")
        if not isinstance(self.account, AccountSnapshot):
            raise TypeError("account must be an AccountSnapshot")
        if self.decision_at is not None:
            decision_at = pd.Timestamp(self.decision_at)
            if decision_at.tz is None:
                raise ValueError("decision_at must be timezone-aware")
            object.__setattr__(self, "decision_at", decision_at.tz_convert("UTC").to_pydatetime())
        if self.market_data is not None and not isinstance(self.market_data, MarketDataView):
            raise TypeError("market_data must be a MarketDataView or None")

    @property
    def available_symbols(self) -> tuple[str, ...]:
        """Symbols with a real, current bar in this event."""
        return tuple(self.bars)

    @property
    def cash(self) -> float:
        return self.account.cash

    @property
    def equity(self) -> float:
        return self.account.equity


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
        limit_price: Optional one-event limit price. Buys fill when the next
            eligible bar reaches the limit; sells fill when it reaches the
            limit. Gap-through simulation fills at the next bar open. In live,
            the same value submits a broker limit order. ``None`` means the
            execution policy's next-eligible-bar open in backtest/sim and a
            broker market order in live.
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
        group_id: Ties this intent to related OrderIntents in the same decision
            (spreads, rolls, inventory hedges). Backtest/sim stage the group as
            one local fill-or-kill unit, and split its failures by who can act
            on them: a leg that cannot fill as written — no executable price, a
            close for a symbol holding nothing, or a close larger than the
            position — raises before anything is staged, while a shortfall the
            venue decides (cash, minimum notional, bar volume, ADV) rolls the
            staged mutation back and reports a group_unfillable runtime event.
            Explicit limit/protective prices outside a configured fixed grid
            fail before staging. An entry leg
            requires an explicit quantity; a close leg may omit one to mean the
            whole position, which is the only way to exit a leg whose size
            changed underneath the strategy. A position carries the group that
            opened it through every later record, so scaling it under another
            identity is refused when the order is planned; a confirmed fill is
            always booked, because attribution is a reconciliation concern and
            not grounds to refuse an execution the venue has made.
            Live preflights and checkpoints the complete group before
            submitting serial broker requests; a later venue failure can still
            leave an already-filled leg, so
            group_id does not claim broker or cross-venue atomicity.
            None means independent execution (the default): the intent may
            wait for its own symbol's next bar without blocking anything else.
        time_in_force: Order lifetime — "day" (rest until the instrument's
            session ends), "gtc" (rest until cancelled), "ioc" (fill what is
            available now, cancel the remainder), or "fok" (fill the entire
            quantity now or cancel it all). None keeps the historical
            one-event opportunity and resolves per order type at the broker
            (see LiveExecutor.OrderRequest).

            Backtest and simulation model these from bar data alone, which
            answers exactly two questions: how many events the order stays
            eligible on, and whether a short fill counts. "Available now"
            means this engine's own participation/notional/cash limits, not
            book depth — queue position is deliberately not modeled. A market
            order resolves on its first eligible event, so its lifetime is
            vacuous; only a limit order can rest. A lifetime is measured from
            the order's first eligible event, not from the bar that emitted
            it. A resting order that fills short does not keep a working
            remainder, and only price eligibility persists: an event that
            priced the order and then refused it for an operational reason has
            resolved it and reports its own decision_skipped reason. Emitting a
            new intent for a symbol that already has a resting order replaces
            it, so "gtc" is never a commitment the strategy cannot escape.

            Live sends the value to the broker, which owns the real lifetime;
            the engine does not simulate resting orders there. Venue limits
            are declared per adapter and checked from preflight once a broker
            is configured, so an unsupported combination fails on the bar that
            emitted it, before any order is built, in backtest as well as
            live; a venue librae does not know is still only checked by its
            own adapter.
    """

    action: OrderAction
    symbol: str = ""
    quantity: float | None = None
    reason: str = ""
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None
    group_id: str | None = None
    time_in_force: TimeInForce | None = None

    def __post_init__(self) -> None:
        if self.action not in ("long", "short", "close"):
            raise ValueError(f"invalid order action: {self.action!r}")
        if not isinstance(self.symbol, str):
            raise TypeError("OrderIntent.symbol must be a string")
        if not isinstance(self.reason, str):
            raise TypeError("OrderIntent.reason must be a string")
        if self.group_id is not None and not isinstance(self.group_id, str):
            raise TypeError("OrderIntent.group_id must be a string or None")
        if self.time_in_force is not None and self.time_in_force not in (
            "day",
            "gtc",
            "ioc",
            "fok",
        ):
            raise ValueError(f"invalid time_in_force: {self.time_in_force!r}")

        if self.quantity is not None:
            _validate_positive_finite_number(self.quantity, "OrderIntent.quantity")

        if self.limit_price is not None:
            _validate_positive_finite_number(self.limit_price, "OrderIntent.limit_price")

        for field_name in ("stop_price", "take_profit_price"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_positive_finite_number(value, f"OrderIntent.{field_name}")
        if self.action == "close" and (
            self.stop_price is not None or self.take_profit_price is not None
        ):
            raise ValueError("close intents cannot set stop_price or take_profit_price")


@dataclass(frozen=True)
class PortfolioWeights:
    """Portfolio-level target weights.

    Positive weights target long exposure and negative weights target short
    exposure. Symbols currently held but absent from ``weights`` target zero
    and are closed. Backtest/sim resolves quantities on the next bar. Live
    sizes at the latest completed close and immediately submits market orders;
    live ``fill_price`` is therefore unsupported.

    Target weights need not sum to one; any remainder stays in cash. Simulated
    execution uses the next eligible bar's open. Live target rebalances submit
    market orders after the completed-bar decision.
    """

    weights: Mapping[str, float]
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise TypeError("PortfolioWeights.reason must be a string")

        weights = dict(self.weights)
        for symbol, raw_weight in weights.items():
            if not isinstance(symbol, str) or not symbol:
                raise ValueError("target weight symbols must be non-empty")
            if isinstance(raw_weight, bool) or not isinstance(raw_weight, Real):
                raise TypeError(f"target weight for {symbol!r} must be numeric")
            if not isfinite(raw_weight):
                raise ValueError(f"target weight for {symbol!r} must be finite")
        object.__setattr__(self, "weights", MappingProxyType(weights))

    def __deepcopy__(self, memo: dict[int, object]) -> PortfolioWeights:
        """Immutable value objects can be shared across runtime snapshots."""
        return self


StrategyDecision = list[OrderIntent] | PortfolioWeights


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
    group_id: str | None = None


class Strategy(ABC):
    """Abstract base for all strategies.

    Strategies only inspect Context and return a decision.
    Data preparation (ETL, signals) is done externally before the backtest.
    Sim/live may retry an event with an equivalent Context after an
    exception, without rolling back mutations to this instance. Runtime checkpoints do not serialize the
    strategy object, so restart-relevant decision state must be reconstructible
    from Context and causal input history rather than mutable instance fields.
    """

    @abstractmethod
    def on_bar(self, ctx: Context) -> StrategyDecision:
        """Return a retry-safe decision for this Context.

        Equivalent same-event calls must not depend on an earlier failed
        call's instance mutation. Return order intents (optionally grouped via
        group_id), weights, or ``[]``.
        """
        ...


def _validate_positive_finite_number(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Real) or not isfinite(value) or value <= 0:
        raise ValueError(f"{field_name} must be positive and finite")
