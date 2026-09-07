"""Execution layer — separates trade execution from engine logic.

Contains:
- simulate_fill(): pure function for simulated fills
- _size_position(): position sizing using all available cash
- calc_trade_pnl(): shared PnL calculation for backtest + live
- scale_into_position(): add to existing position (weighted avg)
- reduce_position(): shrink position after partial close
- close_position(): full or partial close with correct proceeds
- execute_order_intents(): deterministic simulated action loop and live request planner
- TradePnL: PnL breakdown dataclass

Position sizing is the strategy's responsibility (set OrderIntent.quantity).
If strategy doesn't specify quantity, executor uses all available cash
for initial entries only. Scaling requires explicit quantity.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite
from typing import Literal

import numpy as np

from librae.core import EPSILON

from .cost_model import CostModel
from .market_data import CAN_BUY_COLUMN, CAN_SELL_COLUMN
from .run_config import RebalanceResidualPolicy
from .strategy import (
    Fill,
    OrderIntent,
    PortfolioWeights,
    Position,
    PositionEventType,
    PositionSide,
    PositionState,
    StrategyDecision,
    TimeInForce,
)

logger = logging.getLogger(__name__)

# Canonical event_type="close"/"reduce" reasons — engine-generated (not
# strategy-chosen) exits use these exact strings so DB records stay queryable
# without free-text drift. Strategy-chosen closes may use any reason string.
REASON_STOP_LOSS = "stop_loss"
REASON_TAKE_PROFIT = "take_profit"
REASON_FORCE_CLOSE = "force_close"
REASON_DRAWDOWN_BREACH = "drawdown_breach"
REASON_LIQUIDATION = "liquidation"


class ExecutionUnavailableError(ValueError):
    """A deterministic batch cannot execute on this bar; a later one may.

    The category, not any one member of it, is what the engine's bounded
    rebalance deferral catches, so a new reason for "not on this bar" reaches
    the same retry without the loop learning about it. Exhausting the bound --
    including its zero default -- is what makes the condition fatal.

    ``symbols`` names what blocked the batch, for the deferral log and the
    bound-exhaustion message; each subclass owns the wording of why.
    """

    def __init__(self, symbols: list[str], message: str) -> None:
        self.symbols = tuple(sorted(symbols))
        super().__init__(message)


class ExecutionPriceUnavailableError(ExecutionUnavailableError):
    """A deterministic batch cannot resolve every required execution price."""

    def __init__(self, symbols: list[str]) -> None:
        joined = ", ".join(sorted(symbols))
        super().__init__(symbols, f"rebalance requires a valid execution price for {joined}")


class AmbiguousBarOrderingError(ExecutionUnavailableError):
    """This bar cannot order a triggered protection against a non-open fill."""

    def __init__(self, symbols: list[str]) -> None:
        joined = ", ".join(sorted(symbols))
        super().__init__(
            symbols,
            "ambiguous same-bar ordering between non-open pending execution "
            f"and triggered protection for {joined}",
        )


def _intent_order_side(
    intent: OrderIntent,
    position_side: PositionSide | None,
) -> Literal["buy", "sell"] | None:
    if intent.action == "long":
        return "buy"
    if intent.action == "short":
        return "sell"
    if intent.action == "close" and position_side is not None:
        return "sell" if position_side == "long" else "buy"
    return None


def _order_side_is_tradable(
    bar: Mapping[str, object],
    order_side: Literal["buy", "sell"],
) -> bool:
    """Return normalized side tradability without inferring broker rules.

    The data source owns market-specific facts such as price limits, halts,
    auctions, and empty books. The executor consumes only their common result.
    """
    field = CAN_BUY_COLUMN if order_side == "buy" else CAN_SELL_COLUMN
    has_buy = CAN_BUY_COLUMN in bar
    has_sell = CAN_SELL_COLUMN in bar
    if has_buy != has_sell:
        raise ValueError("bar must provide can_buy and can_sell together")
    if not has_buy:
        return True
    value = bar[field]
    if not isinstance(value, (bool, np.bool_)):
        raise ValueError(f"bar {field} must be boolean")
    return bool(value)


def calculate_signed_position_notionals(
    positions: dict[str, PositionState],
    *,
    prices: Mapping[str, float],
    get_cost_model: Callable[[str], CostModel],
) -> dict[str, float]:
    """Return signed market notional per position at one explicit snapshot."""
    if not positions:
        return {}
    missing_prices = sorted(set(positions) - set(prices))
    if missing_prices:
        raise ValueError(f"portfolio exposure validation is missing prices for {missing_prices}")
    invalid_prices = sorted(
        symbol
        for symbol in positions
        if not isfinite(float(prices[symbol])) or float(prices[symbol]) <= 0
    )
    if invalid_prices:
        raise ValueError(f"portfolio exposure validation has invalid prices for {invalid_prices}")
    return {
        symbol: (
            float(prices[symbol])
            * position.quantity
            * get_cost_model(symbol).multiplier
            * side_multiplier(position.side)
        )
        for symbol, position in positions.items()
    }


def calculate_position_weights(
    positions: dict[str, PositionState],
    equity: float,
    *,
    prices: Mapping[str, float],
    get_cost_model: Callable[[str], CostModel],
) -> dict[str, float]:
    """Return signed position weights using the canonical exposure formula."""
    if equity <= EPSILON:
        return {symbol: 0.0 for symbol in positions}
    return {
        symbol: signed_notional / equity
        for symbol, signed_notional in calculate_signed_position_notionals(
            positions,
            prices=prices,
            get_cost_model=get_cost_model,
        ).items()
    }


def _portfolio_risk_values(
    cash: float,
    positions: dict[str, PositionState],
    *,
    prices: Mapping[str, float],
    get_cost_model: Callable[[str], CostModel],
) -> tuple[float, float, float]:
    """Return equity, gross notional, and absolute net notional."""
    signed_notionals = calculate_signed_position_notionals(
        positions,
        prices=prices,
        get_cost_model=get_cost_model,
    )
    equity, _ = calc_equity(
        cash,
        positions,
        get_price=lambda symbol, _position: prices[symbol],
        get_cost_model=get_cost_model,
    )
    return (
        equity,
        sum(abs(notional) for notional in signed_notionals.values()),
        abs(sum(signed_notionals.values())),
    )


def validate_exposure_transition(
    *,
    positions_before: dict[str, PositionState],
    cash_before: float,
    positions_after: dict[str, PositionState],
    cash_after: float,
    prices: Mapping[str, float],
    get_cost_model: Callable[[str], CostModel],
    max_gross_exposure: float | None,
    max_net_exposure: float | None,
) -> None:
    """Reject a decision batch that worsens an already-breached exposure limit."""
    if max_gross_exposure is None and max_net_exposure is None:
        return
    equity_before, gross_notional_before, net_notional_before = _portfolio_risk_values(
        cash_before,
        positions_before,
        prices=prices,
        get_cost_model=get_cost_model,
    )
    equity_after, gross_notional_after, net_notional_after = _portfolio_risk_values(
        cash_after,
        positions_after,
        prices=prices,
        get_cost_model=get_cost_model,
    )
    if equity_before <= EPSILON or equity_after <= EPSILON:
        if (
            max_gross_exposure is not None
            and gross_notional_after > gross_notional_before + EPSILON
        ):
            raise ValueError(
                "post-decision gross notional increases while portfolio equity is non-positive"
            )
        if max_net_exposure is not None and net_notional_after > net_notional_before + EPSILON:
            raise ValueError(
                "post-decision absolute net notional increases while portfolio "
                "equity is non-positive"
            )
        return

    gross_before = gross_notional_before / equity_before
    net_before = net_notional_before / equity_before
    gross_after = gross_notional_after / equity_after
    net_after = net_notional_after / equity_after
    if (
        max_gross_exposure is not None
        and gross_after > max_gross_exposure + EPSILON
        and gross_after > gross_before + EPSILON
    ):
        raise ValueError(
            f"post-decision gross exposure {gross_after:.6f} exceeds "
            f"max_gross_exposure={max_gross_exposure:.6f}"
        )
    if (
        max_net_exposure is not None
        and net_after > max_net_exposure + EPSILON
        and net_after > net_before + EPSILON
    ):
        raise ValueError(
            f"post-decision absolute net exposure {net_after:.6f} exceeds "
            f"max_net_exposure={max_net_exposure:.6f}"
        )


@dataclass(frozen=True)
class TradeResult:
    """Single completed trade — shared by backtest + live engines."""

    symbol: str
    entry_at: datetime
    exit_at: datetime
    side: PositionSide
    entry_price: float
    exit_price: float
    quantity: float
    gross_pnl: float
    commission: float
    slippage: float
    tax: float
    net_pnl: float
    gross_return: float
    net_return: float
    periods_held: int
    group_id: str | None = None


@dataclass(frozen=True)
class TradePnL:
    """PnL breakdown for a single closed trade. Used by backtest + live."""

    gross_pnl: float
    net_pnl: float
    commission: float
    slippage: float
    tax: float
    gross_return: float
    net_return: float
    # Exit-side costs (for cash proceeds calculation)
    exit_commission: float
    exit_slippage: float
    exit_tax: float


@dataclass(frozen=True)
class PositionEvent:
    """Single position lifecycle event with costs from this execution only."""

    ts: datetime
    symbol: str
    side: PositionSide
    event_type: PositionEventType
    fill_quantity: float
    price: float
    entry_price: float
    remaining_quantity: float
    notional: float
    commission: float
    slippage: float
    tax: float
    realized_pnl: float | None = None
    net_return: float | None = None
    entry_at: datetime | None = None
    periods_held: int | None = None
    reason: str = ""
    entry_commission: float | None = None
    entry_slippage: float | None = None
    entry_tax: float | None = None
    group_id: str | None = None
    time_in_force: TimeInForce | None = None
    margin_locked: float | None = None
    leverage: float | None = None
    liquidation_price: float | None = None
    margin_roi: float | None = None
    margin_mode: str | None = None
    cash_flow: float | None = None


RuntimeEventType = Literal["state_recovered", "decision_skipped"]


@dataclass(frozen=True)
class RuntimeEvent:
    """An operational event worth an audit trail — not a fill.

    ``event_type`` is a small, deliberately closed set (matches the
    ``runtime_events`` table's CHECK constraint) — new *kinds* of event
    still extend it deliberately. ``decision_skipped`` instead covers every
    "a decision hit an operational constraint and wasn't executed" case
    (insufficient cash, a notional/volume cap, an unresolvable side, a
    missing quantity, ...) under one type, with ``detail["reason"]``
    naming which one — a free string, not part of the closed set, so a new
    reason never needs a schema change.
    """

    ts: datetime
    event_type: RuntimeEventType
    symbol: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


RebalancePhase = Literal["reduction", "addition"]


@dataclass(frozen=True)
class RebalanceOrderState:
    """One fixed-quantity target leg that may span multiple data events."""

    intent: OrderIntent
    phase: RebalancePhase
    requested_quantity: float
    remaining_quantity: float


@dataclass(frozen=True)
class UnresolvedRebalanceLeg:
    """A fixed target notional waiting for its first fresh execution price."""

    symbol: str
    target_signed_notional: float
    reason: str


@dataclass(frozen=True)
class PortfolioRebalanceState:
    """Backtest-only residual state for one portfolio target."""

    target: PortfolioWeights
    orders: tuple[RebalanceOrderState, ...]
    unresolved_legs: tuple[UnresolvedRebalanceLeg, ...] = ()


def _skipped(
    ts: datetime, reason: str, *, symbol: str | None = None, **context: object
) -> RuntimeEvent:
    """Build a ``decision_skipped`` RuntimeEvent — the single call every
    "operational constraint blocked a decision" site should use.

    ``symbol`` is a top-level field (not just detail) so multiple skips at
    the same ts for different symbols stay distinguishable in storage.
    """
    return RuntimeEvent(
        ts=ts, event_type="decision_skipped", symbol=symbol, detail={"reason": reason, **context}
    )


@dataclass
class ExecutionResult:
    """Results from executing one decision on one bar."""

    trades: list[TradeResult]
    events: list[PositionEvent]
    cash_delta: float
    runtime_events: list[RuntimeEvent] = field(default_factory=list)
    pending_rebalance: PortfolioRebalanceState | None = None


def coalesce_runtime_events(events: list[RuntimeEvent]) -> list[RuntimeEvent]:
    """Preserve all detail under the runtime-events persistence identity."""
    reason_priority = {
        "rebalance_residual": 0,
        "rebalance_constrained_by_position_limit": 1,
        "protective_exit_deferred": 2,
        "rebalance_superseded": 3,
        "rebalance_cancelled_by_protective_exit": 4,
        "rebalance_cancelled_by_halt": 4,
    }
    by_key: dict[tuple[datetime, RuntimeEventType, str | None], int] = {}
    coalesced: list[RuntimeEvent] = []

    def split_detail(detail: Mapping[str, object]) -> tuple[dict[str, object], list[object]]:
        primary = dict(detail)
        raw_related = primary.pop("related_events", [])
        related = list(raw_related) if isinstance(raw_related, list) else []
        return primary, related

    for event in events:
        key = (event.ts, event.event_type, event.symbol)
        existing_index = by_key.get(key)
        if existing_index is None:
            by_key[key] = len(coalesced)
            coalesced.append(event)
            continue
        existing = coalesced[existing_index]
        existing_priority = reason_priority.get(str(existing.detail.get("reason")), 0)
        event_priority = reason_priority.get(str(event.detail.get("reason")), 0)
        existing_detail, existing_related = split_detail(existing.detail)
        event_detail, event_related = split_detail(event.detail)
        if event_priority > existing_priority:
            detail = event_detail
            related = [existing_detail, *existing_related, *event_related]
        else:
            detail = existing_detail
            related = [*existing_related, event_detail, *event_related]
        detail["related_events"] = related
        primary_event = event if event_priority > existing_priority else existing
        coalesced[existing_index] = replace(primary_event, detail=detail)
    return coalesced


def side_multiplier(side: PositionSide) -> float:
    """Convert side to direction multiplier. +1 for long, -1 for short."""
    return -1.0 if side == "short" else 1.0


def _margin_fields(
    entry_price: float,
    remaining_quantity: float,
    side: PositionSide,
    cost_model: CostModel,
) -> tuple[float, float | None, float | None, str]:
    """(margin_locked, leverage, liquidation_price, margin_mode) for the
    position left after an PositionEvent — zero margin_locked/no leverage once
    fully closed (remaining_quantity == 0). margin_mode always reflects the
    side's financing regime (unlevered/fixed/dynamic — see MarginMode), even
    when the position is fully closed."""
    notional = entry_price * remaining_quantity * cost_model.multiplier
    margin_locked = notional * cost_model.margin_rate(side)
    leverage = notional / margin_locked if margin_locked > EPSILON else None
    liq_price = cost_model.liquidation_price(entry_price, side) if remaining_quantity > 0 else None
    return margin_locked, leverage, liq_price, cost_model.margin_mode(side)


def _margin_roi(net_pnl: float, closed_margin: float) -> float | None:
    """PnL as % of the margin backing the closed quantity — real return on
    capital for a leveraged trade, unlike TradePnL.net_return (price return,
    kept notional-based so it stays comparable across trades/runs with
    different margin rates). None when nothing was actually margined
    (closed_margin <= 0, e.g. spot never reaches here with a zero base)."""
    return net_pnl / closed_margin * 100 if closed_margin > EPSILON else None


# ---------------------------------------------------------------------------
# Position snapshot + MTM
# ---------------------------------------------------------------------------


def calc_equity(
    cash: float,
    positions: dict[str, PositionState],
    *,
    get_price: Callable[[str, PositionState], float],
    get_cost_model: Callable[[str], CostModel],
) -> tuple[float, dict[str, Position]]:
    """Compute portfolio MTM value and position snapshot in a single pass.

    Shared by backtest and live engines.

    Args:
        cash: Current cash balance.
        positions: Mutable position states keyed by symbol.
        get_price: (symbol, pos) -> current price for the position.
        get_cost_model: (symbol) -> CostModel for the symbol.

    Returns (mark_to_market, {symbol: Position}).
    """
    mtm = cash
    snapshot: dict[str, Position] = {}
    for sym, ps in positions.items():
        price = get_price(sym, ps)
        cost_model = get_cost_model(sym)
        unrealized = cost_model.calc_pnl(ps.entry_price, price, ps.quantity) * side_multiplier(
            ps.side
        )
        entry_notional = ps.entry_price * ps.quantity * cost_model.multiplier
        mtm += unrealized + entry_notional * cost_model.margin_rate(ps.side)
        snapshot[sym] = Position(
            symbol=sym,
            side=ps.side,
            entry_price=ps.entry_price,
            quantity=ps.quantity,
            entry_at=ps.entry_at,
            periods_held=ps.periods_held,
            unrealized_pnl=unrealized,
            stop_price=ps.stop_price,
            take_profit_price=ps.take_profit_price,
        )
    return mtm, snapshot


# ---------------------------------------------------------------------------
# PnL calculation
# ---------------------------------------------------------------------------


def calc_trade_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: PositionSide,
    cost_model: CostModel,
    entry_commission: float,
    entry_slippage: float,
    entry_tax: float = 0.0,
    exit_bar_volume: float | None = None,
) -> TradePnL:
    """Single trade PnL breakdown. Used by backtest + live."""
    dir_mult = side_multiplier(side)
    gross_pnl = cost_model.calc_pnl(entry_price, exit_price, quantity) * dir_mult

    exit_commission = cost_model.calc_commission(exit_price, quantity)
    exit_slippage = cost_model.calc_slippage(quantity, bar_volume=exit_bar_volume)
    exit_tax = cost_model.calc_tax(exit_price, quantity)

    total_commission = entry_commission + exit_commission
    total_slippage = entry_slippage + exit_slippage
    total_tax = entry_tax + exit_tax
    net_pnl = gross_pnl - total_commission - total_slippage - total_tax

    entry_notional = entry_price * quantity * cost_model.multiplier
    gross_return = (gross_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0
    net_return = (net_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0

    return TradePnL(
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        commission=total_commission,
        slippage=total_slippage,
        tax=total_tax,
        exit_commission=exit_commission,
        exit_slippage=exit_slippage,
        exit_tax=exit_tax,
        gross_return=gross_return,
        net_return=net_return,
    )


# ---------------------------------------------------------------------------
# Position lifecycle
# ---------------------------------------------------------------------------


def scale_into_position(
    pos: PositionState,
    fill: Fill,
    cost_model: CostModel,
) -> None:
    """Scale into an existing position. Mutates pos in place.

    Updates weighted-average entry_price via total_entry_cost to avoid
    float drift on repeated adds (Zipline/QuantConnect pattern).
    """
    add_cost = fill.price * fill.quantity * cost_model.multiplier
    pos.total_entry_cost += add_cost
    pos.quantity += fill.quantity
    pos.entry_price = pos.total_entry_cost / (pos.quantity * cost_model.multiplier)
    pos.entry_commission += fill.commission
    pos.entry_slippage += fill.slippage
    pos.entry_tax += fill.tax


def reduce_position(pos: PositionState, closed_qty: float) -> None:
    """Shrink position after partial close. Mutates pos in place.

    Pro-rates accumulated entry costs by remaining fraction.
    entry_price is unchanged (weighted-average convention).
    """
    remaining = pos.quantity - closed_qty
    if remaining <= EPSILON:
        return
    fraction = remaining / pos.quantity
    pos.quantity = remaining
    pos.total_entry_cost *= fraction
    pos.entry_commission *= fraction
    pos.entry_slippage *= fraction
    pos.entry_tax *= fraction


def close_position(
    pos: PositionState,
    exit_price: float,
    cost_model: CostModel,
    *,
    quantity: float | None = None,
    bar_volume: float | None = None,
) -> tuple[TradePnL, float, bool]:
    """Close a position (full or partial).

    Returns (TradePnL, cash_proceeds, fully_closed).
    """
    close_qty = min(quantity, pos.quantity) if quantity is not None else pos.quantity
    if close_qty <= 0:
        return TradePnL(0, 0, 0, 0, 0, 0, 0, 0, 0, 0), 0.0, False

    fully_closed = close_qty >= pos.quantity - EPSILON

    # Pro-rate entry costs for partial close
    fraction = close_qty / pos.quantity
    pro_entry_commission = pos.entry_commission * fraction
    pro_entry_slippage = pos.entry_slippage * fraction
    pro_entry_tax = pos.entry_tax * fraction

    pnl = calc_trade_pnl(
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=close_qty,
        side=pos.side,
        cost_model=cost_model,
        entry_commission=pro_entry_commission,
        entry_slippage=pro_entry_slippage,
        entry_tax=pro_entry_tax,
        exit_bar_volume=bar_volume,
    )

    # WHY: proceeds = margin_locked + PnL - exit costs.
    # margin_locked = entry_notional * margin_rate (what was deducted on open).
    # Works for all cases: spot long (rate=1.0), spot short, futures.
    entry_notional = pos.entry_price * close_qty * cost_model.multiplier
    margin_locked = entry_notional * cost_model.margin_rate(pos.side)
    exit_costs = pnl.exit_commission + pnl.exit_slippage + pnl.exit_tax
    proceeds = margin_locked + pnl.gross_pnl - exit_costs

    return pnl, proceeds, fully_closed


def build_close_event(
    pos: PositionState,
    ts: datetime,
    exit_price: float,
    cost_model: CostModel,
    reason: str,
    *,
    quantity: float | None = None,
    bar_volume: float | None = None,
    time_in_force: TimeInForce | None = None,
) -> tuple[TradeResult, PositionEvent, float, bool]:
    """Close a position (full/partial) and build its TradeResult + PositionEvent together.

    Single place that turns a "close at this price" decision into the trade
    record + lifecycle event, whoever the caller is (strategy-driven close,
    stop-loss/take-profit trigger, end-of-run force-close). Keeps the three
    call sites from hand-rolling slightly different PositionEvent constructions.

    Returns (trade, event, cash_proceeds, fully_closed).
    """
    close_qty = min(quantity, pos.quantity) if quantity is not None else pos.quantity
    pnl, proceeds, fully_closed = close_position(
        pos,
        exit_price,
        cost_model,
        quantity=close_qty,
        bar_volume=bar_volume,
    )
    trade = build_trade_result(pos, ts, exit_price, close_qty, pnl)
    remaining_qty = 0.0 if fully_closed else max(0.0, pos.quantity - close_qty)
    margin_locked, leverage, liq_price, margin_mode = _margin_fields(
        pos.entry_price, remaining_qty, pos.side, cost_model
    )
    closed_margin, _, _, _ = _margin_fields(pos.entry_price, close_qty, pos.side, cost_model)
    margin_roi = _margin_roi(pnl.net_pnl, closed_margin)
    event = PositionEvent(
        ts=ts,
        symbol=pos.symbol,
        side=pos.side,
        event_type="close" if fully_closed else "reduce",
        fill_quantity=close_qty,
        price=exit_price,
        entry_price=pos.entry_price,
        remaining_quantity=remaining_qty,
        notional=exit_price * close_qty * cost_model.multiplier,
        commission=pnl.exit_commission,
        slippage=pnl.exit_slippage,
        tax=pnl.exit_tax,
        entry_commission=pnl.commission - pnl.exit_commission,
        entry_slippage=pnl.slippage - pnl.exit_slippage,
        entry_tax=pnl.tax - pnl.exit_tax,
        realized_pnl=pnl.net_pnl,
        net_return=pnl.net_return,
        entry_at=pos.entry_at,
        periods_held=pos.periods_held,
        reason=reason,
        group_id=pos.group_id,
        time_in_force=time_in_force,
        margin_locked=margin_locked,
        leverage=leverage,
        liquidation_price=liq_price,
        margin_roi=margin_roi,
        margin_mode=margin_mode,
        cash_flow=proceeds,
    )
    return trade, event, proceeds, fully_closed


def apply_execution_fill(
    positions: dict[str, PositionState],
    cash: float,
    fill: Fill,
    ts: datetime,
    *,
    order_side: Literal["buy", "sell"],
    cost_model: CostModel,
    reason: str = "",
    group_id: str | None = None,
    time_in_force: TimeInForce | None = None,
) -> tuple[float, ExecutionResult]:
    """Apply one externally confirmed execution to portfolio state.

    Unlike :func:`execute_order_intents`, this function does not simulate price,
    quantity, or costs. ``fill`` must already contain the execution venue's
    confirmed average price, filled quantity, and cash-denominated costs.
    The order side plus current position determines whether the fill opens,
    adds, reduces, or closes exposure.

    Crossing through an existing position is rejected. Strategies must close
    first and open the opposite side with a separate order, which keeps every
    fill's lifecycle and PnL attribution unambiguous.
    """
    numeric_values = (
        fill.price,
        fill.quantity,
        fill.commission,
        fill.slippage,
        fill.tax,
    )
    if (
        not all(isfinite(value) for value in numeric_values)
        or fill.price <= 0
        or fill.quantity <= 0
        or min(fill.slippage, fill.tax) < 0
    ):
        raise ValueError(
            "execution fill must contain positive price/quantity and non-negative slippage/tax"
        )

    symbol = fill.symbol
    position = positions.get(symbol)
    entry_side: Literal["long", "short"] = "long" if order_side == "buy" else "short"
    costs = fill.commission + fill.slippage + fill.tax
    notional = fill.price * fill.quantity * cost_model.multiplier

    if position is None or position.side == entry_side:
        outlay = notional * cost_model.margin_rate(entry_side) + costs
        event_type: Literal["open", "add"] = "open" if position is None else "add"
        if position is None:
            position = PositionState(
                symbol=symbol,
                side=entry_side,
                entry_price=fill.price,
                quantity=fill.quantity,
                entry_at=ts,
                periods_held=0,
                entry_commission=fill.commission,
                entry_slippage=fill.slippage,
                entry_tax=fill.tax,
                total_entry_cost=notional,
                group_id=group_id,
            )
            positions[symbol] = position
        else:
            scale_into_position(position, fill, cost_model)

        margin_locked, leverage, liq_price, margin_mode = _margin_fields(
            position.entry_price, position.quantity, entry_side, cost_model
        )
        event = PositionEvent(
            ts=ts,
            symbol=symbol,
            side=entry_side,
            event_type=event_type,
            fill_quantity=fill.quantity,
            price=fill.price,
            entry_price=position.entry_price,
            remaining_quantity=position.quantity,
            notional=notional,
            commission=fill.commission,
            slippage=fill.slippage,
            tax=fill.tax,
            reason=reason,
            group_id=group_id,
            time_in_force=time_in_force,
            entry_at=position.entry_at,
            margin_locked=margin_locked,
            leverage=leverage,
            liquidation_price=liq_price,
            margin_mode=margin_mode,
            cash_flow=-outlay,
        )
        result = ExecutionResult(trades=[], events=[event], cash_delta=-outlay)
        return cash - outlay, result

    if fill.quantity > position.quantity + EPSILON:
        raise ValueError(
            f"execution fill would cross {symbol} from {position.side}: "
            f"filled={fill.quantity}, open={position.quantity}"
        )

    close_quantity = min(fill.quantity, position.quantity)
    fully_closed = close_quantity >= position.quantity - EPSILON
    fraction = close_quantity / position.quantity
    entry_commission = position.entry_commission * fraction
    entry_slippage = position.entry_slippage * fraction
    entry_tax = position.entry_tax * fraction
    gross_pnl = cost_model.calc_pnl(
        position.entry_price, fill.price, close_quantity
    ) * side_multiplier(position.side)
    total_commission = entry_commission + fill.commission
    total_slippage = entry_slippage + fill.slippage
    total_tax = entry_tax + fill.tax
    net_pnl = gross_pnl - total_commission - total_slippage - total_tax
    entry_notional = position.entry_price * close_quantity * cost_model.multiplier
    gross_return = gross_pnl / entry_notional * 100 if entry_notional > EPSILON else 0.0
    net_return = net_pnl / entry_notional * 100 if entry_notional > EPSILON else 0.0
    pnl = TradePnL(
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        commission=total_commission,
        slippage=total_slippage,
        tax=total_tax,
        exit_commission=fill.commission,
        exit_slippage=fill.slippage,
        exit_tax=fill.tax,
        gross_return=gross_return,
        net_return=net_return,
    )
    trade = build_trade_result(position, ts, fill.price, close_quantity, pnl)
    remaining_quantity = 0.0 if fully_closed else position.quantity - close_quantity
    margin_locked, leverage, liq_price, margin_mode = _margin_fields(
        position.entry_price, remaining_quantity, position.side, cost_model
    )
    closed_margin = entry_notional * cost_model.margin_rate(position.side)
    margin_roi = _margin_roi(net_pnl, closed_margin)
    proceeds = closed_margin + gross_pnl - costs
    event = PositionEvent(
        ts=ts,
        symbol=symbol,
        side=position.side,
        event_type="close" if fully_closed else "reduce",
        fill_quantity=close_quantity,
        price=fill.price,
        entry_price=position.entry_price,
        remaining_quantity=remaining_quantity,
        notional=notional,
        commission=fill.commission,
        slippage=fill.slippage,
        tax=fill.tax,
        entry_commission=entry_commission,
        entry_slippage=entry_slippage,
        entry_tax=entry_tax,
        realized_pnl=net_pnl,
        net_return=net_return,
        entry_at=position.entry_at,
        periods_held=position.periods_held,
        reason=reason,
        group_id=position.group_id,
        time_in_force=time_in_force,
        margin_locked=margin_locked,
        leverage=leverage,
        liquidation_price=liq_price,
        margin_roi=margin_roi,
        margin_mode=margin_mode,
        cash_flow=proceeds,
    )

    if fully_closed:
        del positions[symbol]
    else:
        reduce_position(position, close_quantity)

    result = ExecutionResult(trades=[trade], events=[event], cash_delta=proceeds)
    return cash + proceeds, result


# ---------------------------------------------------------------------------
# Stop-loss / take-profit
# ---------------------------------------------------------------------------


def resolve_stop_exit(
    pos: PositionState,
    bar: dict[str, float],
    cost_model: CostModel,
) -> tuple[float, str] | None:
    """Check whether this bar's range triggers pos's liquidation, stop-loss,
    or take-profit.

    Liquidation is checked first: it's the hardest, most conservative
    constraint a real exchange enforces — if it triggers, no soft stop
    order would have executed first in reality, so it always wins over a
    stop/TP that would also trigger the same bar. It's modeled the same
    way as stop_price: fills at the *worse* of (liquidation_price, bar
    open) to capture gap-through risk. Disabled (never triggers) unless
    cost_model.maintenance_margin_rate is set — see CostModel.liquidation_price.

    Otherwise: stop_price is modeled as a stop-market order (worse-of-gap
    fill); take_profit_price is modeled as a limit order (target price once
    touched, or a better opening price after a favorable gap). Stop-loss is
    checked before take-profit — if both would trigger on the same bar, the
    conservative outcome wins.
    A previously triggered, volume-limited market exit continues at this
    bar's open without checking the trigger level again.

    Returns (fill_price, reason) or None if nothing is triggered.
    """
    high, low, open_ = bar.get("high"), bar.get("low"), bar.get("open")
    if high is None or low is None or open_ is None:
        return None
    is_long = pos.side == "long"

    if pos.pending_market_exit_reason is not None:
        return open_, pos.pending_market_exit_reason

    liq_price = cost_model.liquidation_price(pos.entry_price, pos.side)
    if liq_price is not None:
        triggered = low <= liq_price if is_long else high >= liq_price
        if triggered:
            fill = min(liq_price, open_) if is_long else max(liq_price, open_)
            return fill, REASON_LIQUIDATION

    if pos.stop_price is not None:
        triggered = low <= pos.stop_price if is_long else high >= pos.stop_price
        if triggered:
            fill = min(pos.stop_price, open_) if is_long else max(pos.stop_price, open_)
            return fill, REASON_STOP_LOSS

    if pos.take_profit_price is not None:
        triggered = high >= pos.take_profit_price if is_long else low <= pos.take_profit_price
        if triggered:
            fill = (
                max(pos.take_profit_price, open_) if is_long else min(pos.take_profit_price, open_)
            )
            return fill, REASON_TAKE_PROFIT

    return None


def check_stop_targets(
    positions: dict[str, PositionState],
    bars: dict[str, dict[str, float]],
    ts: datetime,
    *,
    get_cost_model: Callable[[str], CostModel],
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
    eligible_symbols: set[str] | None = None,
) -> ExecutionResult:
    """Force-close any position whose stop-loss/take-profit is hit this bar.

    Used by backtest/sim — called once per bar, before the
    strategy sees this bar's Context, so a triggered stop is filled and
    reflected in the same bar's equity (real stop orders don't wait a bar).
    Mutates *positions* in place.
    """
    trades: list[TradeResult] = []
    events: list[PositionEvent] = []
    runtime_events: list[RuntimeEvent] = []
    cash_delta = 0.0

    for sym in list(positions.keys()):
        if eligible_symbols is not None and sym not in eligible_symbols:
            continue
        bar = bars.get(sym)
        if bar is None:
            continue
        pos = positions[sym]
        cost_model = get_cost_model(sym)
        hit = resolve_stop_exit(pos, bar, cost_model)
        if hit is None:
            continue
        price, reason = hit
        close_side: Literal["buy", "sell"] = "sell" if pos.side == "long" else "buy"
        if not _order_side_is_tradable(bar, close_side):
            if reason in (REASON_LIQUIDATION, REASON_STOP_LOSS):
                pos.pending_market_exit_reason = reason
            runtime_events.append(
                _skipped(ts, "protective_exit_deferred", symbol=sym, exit_reason=reason)
            )
            logger.info(
                "%s exit for %s remains pending because the order side is not tradable",
                reason,
                sym,
            )
            continue
        bar_volume = bar.get("volume")
        max_volume_qty = _volume_fill_limit(
            sym,
            max_bar_volume_participation_rate,
            bar_volume,
            max_adv_participation_rate=max_adv_participation_rate,
            lagged_adv=get_lagged_adv(sym) if get_lagged_adv else None,
            used_bar_quantity=(used_bar_quantity_by_symbol or {}).get(sym, 0.0),
            used_adv_quantity=(used_adv_quantity_by_symbol or {}).get(sym, 0.0),
        )
        close_quantity = pos.quantity
        if max_volume_qty is not None:
            close_quantity = min(close_quantity, max_volume_qty)
        if close_quantity <= EPSILON:
            if reason in (REASON_LIQUIDATION, REASON_STOP_LOSS):
                pos.pending_market_exit_reason = reason
            runtime_events.append(
                _skipped(ts, "protective_exit_deferred", symbol=sym, exit_reason=reason)
            )
            continue
        trade, event, proceeds, fully_closed = build_close_event(
            pos,
            ts,
            price,
            cost_model,
            reason,
            quantity=close_quantity,
            bar_volume=bar_volume,
        )
        trades.append(trade)
        events.append(event)
        if used_bar_quantity_by_symbol is not None:
            used_bar_quantity_by_symbol[sym] = (
                used_bar_quantity_by_symbol.get(sym, 0.0) + close_quantity
            )
        if used_adv_quantity_by_symbol is not None:
            used_adv_quantity_by_symbol[sym] = (
                used_adv_quantity_by_symbol.get(sym, 0.0) + close_quantity
            )
        cash_delta += proceeds
        if fully_closed:
            del positions[sym]
        else:
            if reason in (REASON_LIQUIDATION, REASON_STOP_LOSS):
                pos.pending_market_exit_reason = reason
            reduce_position(pos, close_quantity)

    return ExecutionResult(
        trades=trades,
        events=events,
        cash_delta=cash_delta,
        runtime_events=runtime_events,
    )


def queue_market_exit_all(
    positions: dict[str, PositionState],
    *,
    reason: str,
) -> None:
    """Queue market exits for the next observed tradable bar."""
    for position in positions.values():
        position.pending_market_exit_reason = reason


def liquidate_all(
    positions: dict[str, PositionState],
    bars: dict[str, dict[str, float]],
    ts: datetime,
    *,
    get_cost_model: Callable[[str], CostModel],
    reason: str,
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
) -> ExecutionResult:
    """Force-close every open position right now, at this bar's close price.

    Mutates *positions* in place. A position the bar's liquidity budget
    cannot absorb is closed partially — or not at all — and stays in
    *positions*; there is no later bar to retry on, so every residual is
    reported as a ``decision_skipped`` event with reason
    ``force_close_incomplete``.
    """
    trades: list[TradeResult] = []
    events: list[PositionEvent] = []
    runtime_events: list[RuntimeEvent] = []
    cash_delta = 0.0

    for sym in list(positions.keys()):
        pos = positions[sym]
        bar = bars.get(sym)
        if bar is None or bar.get("close") is None:
            continue
        close_side: Literal["buy", "sell"] = "sell" if pos.side == "long" else "buy"
        if not _order_side_is_tradable(bar, close_side):
            logger.info(
                "Forced exit for %s cannot fill because the order side is not tradable",
                sym,
            )
            continue
        price = bar["close"]
        bar_volume = bar.get("volume")
        max_volume_qty = _volume_fill_limit(
            sym,
            max_bar_volume_participation_rate,
            bar_volume,
            max_adv_participation_rate=max_adv_participation_rate,
            lagged_adv=get_lagged_adv(sym) if get_lagged_adv else None,
            used_bar_quantity=(used_bar_quantity_by_symbol or {}).get(sym, 0.0),
            used_adv_quantity=(used_adv_quantity_by_symbol or {}).get(sym, 0.0),
        )
        close_quantity = pos.quantity
        if max_volume_qty is not None:
            close_quantity = min(close_quantity, max_volume_qty)
        if close_quantity <= EPSILON:
            continue
        cost_model = get_cost_model(sym)
        trade, event, proceeds, fully_closed = build_close_event(
            pos,
            ts,
            price,
            cost_model,
            reason,
            quantity=close_quantity,
            bar_volume=bar_volume,
        )
        trades.append(trade)
        events.append(event)
        if used_bar_quantity_by_symbol is not None:
            used_bar_quantity_by_symbol[sym] = (
                used_bar_quantity_by_symbol.get(sym, 0.0) + close_quantity
            )
        if used_adv_quantity_by_symbol is not None:
            used_adv_quantity_by_symbol[sym] = (
                used_adv_quantity_by_symbol.get(sym, 0.0) + close_quantity
            )
        cash_delta += proceeds
        if fully_closed:
            del positions[sym]
        else:
            reduce_position(pos, close_quantity)

    for sym, pos in positions.items():
        runtime_events.append(
            _skipped(ts, "force_close_incomplete", symbol=sym, remaining_quantity=pos.quantity)
        )

    return ExecutionResult(
        trades=trades, events=events, cash_delta=cash_delta, runtime_events=runtime_events
    )


# ---------------------------------------------------------------------------
# Simulated fill price resolution (backtest/sim)
# ---------------------------------------------------------------------------


def resolve_fill_price(
    bar: dict[str, float],
    intent: OrderIntent,
    default_fill: str,
    *,
    position_side: PositionSide | None = None,
) -> float | None:
    """Resolve a limit price or the engine's simulated market fill field.

    Args:
        bar: Next bar's OHLCV dict (the bar where the fill happens).
        intent: The order intent whose fill-price specification to use.
        default_fill: Engine-level default field name (e.g. "open").
        position_side: Required to infer the order direction for a close.

    Returns:
        Resolved price, or None if the order should be rejected
        (limit not reachable, field missing/zero).

    Numeric fill_spec models a resting limit order with full bar liquidity.
    A buy fills when low reaches the limit; a sell fills when high reaches it.
    A gap through the limit receives the opening price, otherwise the limit.
    The intent is good for this eligible bar only.
    """
    fill_spec: float | str = intent.limit_price if intent.limit_price is not None else default_fill
    order_side = _intent_order_side(intent, position_side)
    if order_side is None:
        logger.warning("Order rejected: cannot infer order side for %s", intent.action)
        return None
    if not _order_side_is_tradable(bar, order_side):
        logger.info(
            "%s %s cannot fill because the order side is not tradable",
            order_side,
            intent.symbol,
        )
        return None

    if isinstance(fill_spec, (int, float)):
        limit = float(fill_spec)
        if not isfinite(limit) or limit <= 0:
            return None

        low_raw, high_raw = bar.get("low"), bar.get("high")
        if low_raw is None or high_raw is None:
            logger.warning("Limit order rejected: bar is missing low/high")
            return None
        low, high = float(low_raw), float(high_raw)
        if not isfinite(low) or not isfinite(high) or low <= 0 or high <= 0 or low > high:
            logger.warning("Limit order rejected: bar has invalid low/high")
            return None
        open_raw = bar.get("open")
        if open_raw is None:
            logger.warning("Limit order rejected: bar is missing open")
            return None
        open_price = float(open_raw)
        if not isfinite(open_price) or open_price <= 0:
            logger.warning("Limit order rejected: bar has invalid open")
            return None

        reached = low <= limit if order_side == "buy" else high >= limit
        if not reached:
            logger.info(
                "Limit intent expired unfilled: %s %s at %.6f",
                order_side,
                intent.symbol,
                limit,
            )
            return None
        return min(open_price, limit) if order_side == "buy" else max(open_price, limit)

    if isinstance(fill_spec, str):
        val = bar.get(fill_spec)
        if val is not None and float(val) > 0:
            return float(val)
        logger.warning("fill_price='%s' not found or zero in bar, order rejected", fill_spec)
    return None


# ---------------------------------------------------------------------------
# Fill creation + sizing
# ---------------------------------------------------------------------------


def _size_position(
    cost_model: CostModel,
    price: float,
    cash: float,
    side: PositionSide,
    *,
    bar_volume: float | None = None,
) -> float:
    """Largest quantity whose estimate_entry_outlay fits in cash.

    Solved directly rather than by pricing 1 unit and extrapolating linearly:
    min_commission is a flat per-trade floor, not a per-unit cost, so a
    1-unit outlay estimate prices it as if it were charged on every unit —
    massively undersizing the position once the real (much larger) quantity
    would clear the floor via the rate-based commission alone.
    """
    notional_per_unit = price * cost_model.multiplier
    linear = (
        notional_per_unit * (cost_model.margin_rate(side) + cost_model.tax_rate)
        + cost_model.slippage_ticks * cost_model.tick_size * cost_model.multiplier
    )
    if linear < EPSILON:
        return 0.0
    marginal_commission = notional_per_unit * cost_model.commission_rate

    # Below breakeven_qty, commission is pinned at the flat floor; above it,
    # commission scales with quantity. Solve in whichever regime `cash`
    # actually falls into.
    breakeven_qty = (
        cost_model.min_commission / marginal_commission
        if marginal_commission > EPSILON
        else float("inf")
    )
    if cash <= linear * breakeven_qty + cost_model.min_commission:
        qty = (cash - cost_model.min_commission) / linear
    else:
        qty = cash / (linear + marginal_commission)
    qty = max(qty, 0.0)
    if (
        qty > EPSILON
        and bar_volume is not None
        and bar_volume > 0
        and cost_model.volume_impact_ticks > 0
        and cost_model.estimate_entry_outlay(
            price,
            qty,
            side,
            bar_volume=bar_volume,
        )
        > cash
    ):
        low, high = 0.0, qty
        for _ in range(60):
            middle = (low + high) / 2.0
            outlay = cost_model.estimate_entry_outlay(
                price,
                middle,
                side,
                bar_volume=bar_volume,
            )
            if outlay <= cash:
                low = middle
            else:
                high = middle
        qty = low
    return qty


def _shrink_fill(
    fill: Fill,
    cost_model: CostModel,
    target_qty: float,
    bar_volume: float | None,
) -> Fill | None:
    """Rebuild a fill at a smaller target_qty, recomputing commission/
    slippage/tax — they scale with quantity, so a naive quantity clamp
    would overcharge relative to what's actually being filled. Returns
    None if target_qty <= 0, or fill unchanged if target_qty isn't smaller.
    """
    if target_qty <= EPSILON:
        return None
    if target_qty >= fill.quantity - EPSILON:
        return fill
    return Fill(
        symbol=fill.symbol,
        side=fill.side,
        price=fill.price,
        quantity=target_qty,
        commission=cost_model.calc_commission(fill.price, target_qty),
        slippage=cost_model.calc_slippage(target_qty, bar_volume=bar_volume),
        tax=cost_model.calc_tax(fill.price, target_qty),
    )


def _cap_fill_to_notional(
    fill: Fill,
    existing_qty: float,
    cost_model: CostModel,
    max_notional: float,
    *,
    bar_volume: float | None = None,
) -> Fill | None:
    """Shrink a fill so (existing_qty + fill.quantity) * price * multiplier
    stays within max_notional. Returns None if there's no room at all
    (existing position already at/over the cap).
    """
    unit_notional = fill.price * cost_model.multiplier
    room = max_notional - existing_qty * unit_notional
    if room <= EPSILON:
        return None
    target_qty = min(fill.quantity, room / unit_notional)
    capped = _shrink_fill(fill, cost_model, target_qty, bar_volume)
    if capped is not None and capped is not fill:
        logger.info(
            "Position cap: %s %s clamped qty %.6f -> %.6f (max_notional=%.2f)",
            fill.side,
            fill.symbol,
            fill.quantity,
            capped.quantity,
            max_notional,
        )
    return capped


def _cap_fill_to_volume(
    fill: Fill,
    cost_model: CostModel,
    max_qty: float,
    *,
    bar_volume: float | None = None,
) -> Fill | None:
    """Shrink a fill to at most max_qty (typically max_bar_volume_participation_rate
    * bar_volume) — a per-fill "how much of this bar's liquidity can I touch"
    constraint, unlike the notional cap which accumulates across a position.
    """
    target_qty = min(fill.quantity, max_qty)
    capped = _shrink_fill(fill, cost_model, target_qty, bar_volume)
    if capped is not None and capped is not fill:
        logger.info(
            "Volume cap: %s %s clamped qty %.6f -> %.6f (max_qty=%.6f)",
            fill.side,
            fill.symbol,
            fill.quantity,
            capped.quantity,
            max_qty,
        )
    return capped


def _volume_fill_limit(
    symbol: str,
    max_bar_volume_participation_rate: float | None,
    bar_volume: float | None,
    *,
    max_adv_participation_rate: float | None = None,
    lagged_adv: float | None = None,
    used_bar_quantity: float = 0.0,
    used_adv_quantity: float = 0.0,
) -> float | None:
    """Return the tightest configured liquidity budget for this event."""
    remaining_budgets: list[float] = []
    if max_bar_volume_participation_rate is not None:
        if bar_volume is None or not isfinite(bar_volume) or bar_volume < 0:
            logger.warning(
                "Fill rejected for %s: max_bar_volume_participation_rate requires "
                "finite non-negative bar volume",
                symbol,
            )
            return 0.0
        remaining_budgets.append(
            max(max_bar_volume_participation_rate * bar_volume - used_bar_quantity, 0.0)
        )

    if max_adv_participation_rate is not None:
        if lagged_adv is None or not isfinite(lagged_adv) or lagged_adv < 0:
            logger.warning(
                "Fill rejected for %s: max_adv_participation_rate requires "
                "a complete lagged ADV window",
                symbol,
            )
            return 0.0
        remaining_budgets.append(
            max(max_adv_participation_rate * lagged_adv - used_adv_quantity, 0.0)
        )

    if not remaining_budgets:
        return None
    return min(remaining_budgets)


def simulate_fill(
    intent: OrderIntent,
    price: float,
    cash: float,
    cost_model: CostModel,
    *,
    bar_volume: float | None = None,
) -> Fill | None:
    """Build a Fill for a long/short intent. Returns None if rejected."""
    # Both guards below are unreachable via _try_fill's current callers —
    # execute_order_intents only reaches this path for action in
    # ("long", "short"), and OrderIntent.__post_init__ already rejects a
    # non-positive quantity at construction. Kept as a defensive contract
    # for any future direct caller of this function; _try_fill's caller-
    # facing "insufficient_cash" reason assumes only the sizing path below
    # (qty computed by _size_position) can actually produce qty <= 0.
    if intent.action not in ("long", "short"):
        return None

    qty = intent.quantity
    if qty is None:
        qty = _size_position(
            cost_model,
            price,
            cash,
            intent.action,
            bar_volume=bar_volume,
        )
    if qty <= 0:
        return None

    return Fill(
        symbol=intent.symbol,
        side=intent.action,
        price=price,
        quantity=qty,
        commission=cost_model.calc_commission(price, qty),
        slippage=cost_model.calc_slippage(qty, bar_volume=bar_volume),
        tax=cost_model.calc_tax(price, qty),
    )


def build_trade_result(
    pos: PositionState,
    exit_at: datetime,
    exit_price: float,
    close_qty: float,
    pnl: TradePnL,
) -> TradeResult:
    """Build a TradeResult from a (partial or full) close."""
    return TradeResult(
        symbol=pos.symbol,
        entry_at=pos.entry_at,
        exit_at=exit_at,
        side=pos.side,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=close_qty,
        gross_pnl=pnl.gross_pnl,
        commission=pnl.commission,
        slippage=pnl.slippage,
        tax=pnl.tax,
        net_pnl=pnl.net_pnl,
        gross_return=pnl.gross_return,
        net_return=pnl.net_return,
        periods_held=pos.periods_held,
        group_id=pos.group_id,
    )


# ---------------------------------------------------------------------------
# Deterministic action processing (fills backtest/sim; plans live requests on a copy)
# ---------------------------------------------------------------------------


def _try_fill(
    action: OrderIntent,
    price: float,
    available_cash: float,
    cost_model: CostModel,
    *,
    max_notional: float | None = None,
    existing_qty: float = 0.0,
    max_volume_qty: float | None = None,
    bar_volume: float | None = None,
) -> tuple[Fill | None, float, str | None]:
    """Attempt a fill and validate cash sufficiency.

    Returns (fill, outlay, None), or (None, 0.0, skip_reason) when rejected.
    """
    fill = simulate_fill(action, price, available_cash, cost_model, bar_volume=bar_volume)
    if not fill or fill.quantity <= 0:
        return None, 0.0, "insufficient_cash"
    if max_notional is not None:
        fill = _cap_fill_to_notional(
            fill, existing_qty, cost_model, max_notional, bar_volume=bar_volume
        )
        if fill is None:
            return None, 0.0, "notional_capped"
    if max_volume_qty is not None:
        fill = _cap_fill_to_volume(fill, cost_model, max_volume_qty, bar_volume=bar_volume)
        if fill is None:
            return None, 0.0, "volume_capped"
    outlay = cost_model.estimate_entry_outlay(
        price,
        fill.quantity,
        action.action,
        bar_volume=bar_volume,
    )
    if available_cash - outlay < -EPSILON:
        return None, 0.0, "insufficient_cash"
    return fill, outlay, None


def _validate_entry_order_notional(
    symbol: str,
    fill: Fill,
    cost_model: CostModel,
    max_order_notional: float | None,
) -> None:
    """Reject an exposure-increasing fill above the configured order-size limit."""
    if max_order_notional is None:
        return
    notional = fill.price * fill.quantity * cost_model.multiplier
    if notional > max_order_notional + EPSILON:
        raise ValueError(
            f"{symbol} entry order notional {notional:.6f} exceeds "
            f"max_order_notional={max_order_notional:.6f}"
        )


def execute_order_intents(
    intents: list[OrderIntent],
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    get_price: Callable[[str, OrderIntent], float | None],
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_order_notional: float | None = None,
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
    atomic_groups: bool = False,
) -> ExecutionResult:
    """Execute symbol-level intents: open, scale, partial/full close.

    Mutates *positions* dict in place. Backtest/sim uses the resulting fills;
    live may use it only on a copied portfolio to size order requests.

    max_position_notional, when set, caps every symbol's post-fill notional
    (existing + added) to this value — applied identically to new entries
    and scale-ins.

    max_bar_volume_participation_rate gives each symbol one cumulative budget for
    this data event across entries, additions, reductions, and closes. Missing
    volume rejects the fill when this limit is enabled. get_volume also feeds
    CostModel.calc_slippage's participation-scaled impact component. The
    optional ADV limit uses a separate counter that accumulates across every
    bar in the current trading session.

    ``atomic_groups`` is enabled by the backtest/simulation path. Each
    non-None ``group_id`` then uses fill-or-kill semantics: every member must
    fill its entire explicit quantity, otherwise all staged fills and
    liquidity usage for that group are discarded. Live planning deliberately
    leaves this disabled and performs broker-specific group handling itself.
    """
    _validate_scale_in_group_identity(intents, positions, primary_symbol)
    if atomic_groups and any(intent.group_id is not None for intent in intents):
        return _execute_order_intent_groups_atomically(
            intents,
            positions,
            cash,
            ts,
            get_price=get_price,
            get_cost_model=get_cost_model,
            primary_symbol=primary_symbol,
            max_position_notional=max_position_notional,
            max_order_notional=max_order_notional,
            max_bar_volume_participation_rate=max_bar_volume_participation_rate,
            max_adv_participation_rate=max_adv_participation_rate,
            get_volume=get_volume,
            get_lagged_adv=get_lagged_adv,
            used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
            used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
        )

    trades: list[TradeResult] = []
    events: list[PositionEvent] = []
    runtime_events: list[RuntimeEvent] = []
    cash_delta = 0.0
    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}

    # Resolve every intent's price once so grouped intents can be checked for
    # all-or-nothing pricing before any of them (or an unrelated intent) fills.
    priced_actions = [
        (action, get_price(action.symbol or primary_symbol, action)) for action in intents
    ]
    groups: dict[str, list[tuple[OrderIntent, float | None]]] = {}
    for action, price_raw in priced_actions:
        if action.group_id is not None:
            groups.setdefault(action.group_id, []).append((action, price_raw))
    for group_id, members in groups.items():
        missing = sorted(
            {
                action.symbol or primary_symbol
                for action, price_raw in members
                if price_raw is None or price_raw <= 0
            }
        )
        if missing:
            raise ValueError(
                f"group {group_id!r} lost pricing for {missing} between decision and "
                "execution; cannot fill some legs and not others"
            )

    for action, price_raw in priced_actions:
        sym = action.symbol or primary_symbol
        if price_raw is None or price_raw <= 0:
            continue
        price = float(price_raw)
        cost_model = get_cost_model(sym)
        reason = action.reason
        group_id = action.group_id
        time_in_force = action.time_in_force

        if action.action in ("long", "short"):
            desired_side: PositionSide = action.action

            bar_volume = get_volume(sym) if get_volume else None
            max_volume_qty = _volume_fill_limit(
                sym,
                max_bar_volume_participation_rate,
                bar_volume,
                max_adv_participation_rate=max_adv_participation_rate,
                lagged_adv=get_lagged_adv(sym) if get_lagged_adv else None,
                used_bar_quantity=volume_consumed.get(sym, 0.0),
                used_adv_quantity=adv_consumed.get(sym, 0.0),
            )

            if sym not in positions:
                # OPEN NEW
                fill, outlay, skip_reason = _try_fill(
                    action,
                    price,
                    cash + cash_delta,
                    cost_model,
                    max_notional=max_position_notional,
                    existing_qty=0.0,
                    max_volume_qty=max_volume_qty,
                    bar_volume=bar_volume,
                )
                if fill:
                    _validate_entry_order_notional(
                        sym,
                        fill,
                        cost_model,
                        max_order_notional,
                    )
                    cash_delta -= outlay
                    positions[sym] = PositionState(
                        symbol=sym,
                        side=fill.side,
                        entry_price=price,
                        quantity=fill.quantity,
                        entry_at=ts,
                        periods_held=0,
                        entry_commission=fill.commission,
                        entry_slippage=fill.slippage,
                        entry_tax=fill.tax,
                        total_entry_cost=price * fill.quantity * cost_model.multiplier,
                        stop_price=action.stop_price,
                        take_profit_price=action.take_profit_price,
                        group_id=group_id,
                    )
                    margin_locked, leverage, liq_price, margin_mode = _margin_fields(
                        price, fill.quantity, fill.side, cost_model
                    )
                    events.append(
                        PositionEvent(
                            ts=ts,
                            symbol=sym,
                            side=fill.side,
                            event_type="open",
                            fill_quantity=fill.quantity,
                            price=price,
                            entry_price=price,
                            remaining_quantity=fill.quantity,
                            notional=price * fill.quantity * cost_model.multiplier,
                            commission=fill.commission,
                            slippage=fill.slippage,
                            tax=fill.tax,
                            reason=reason,
                            group_id=group_id,
                            time_in_force=time_in_force,
                            entry_at=positions[sym].entry_at,
                            margin_locked=margin_locked,
                            leverage=leverage,
                            liquidation_price=liq_price,
                            margin_mode=margin_mode,
                            cash_flow=-outlay,
                        )
                    )
                    volume_consumed[sym] = volume_consumed.get(sym, 0.0) + fill.quantity
                    adv_consumed[sym] = adv_consumed.get(sym, 0.0) + fill.quantity
                else:
                    runtime_events.append(_skipped(ts, skip_reason, symbol=sym))

            elif positions[sym].side == desired_side:
                # SCALE IN — must specify quantity. Same severity as the
                # opposite-side rejection below: both are a strategy action
                # silently turned into a no-op, not a normal/expected path.
                if action.quantity is None:
                    logger.warning("Scaling %s requires explicit quantity, skipping", sym)
                    runtime_events.append(_skipped(ts, "missing_quantity", symbol=sym))
                    continue
                fill, outlay, skip_reason = _try_fill(
                    action,
                    price,
                    cash + cash_delta,
                    cost_model,
                    max_notional=max_position_notional,
                    existing_qty=positions[sym].quantity,
                    max_volume_qty=max_volume_qty,
                    bar_volume=bar_volume,
                )
                if fill:
                    _validate_entry_order_notional(
                        sym,
                        fill,
                        cost_model,
                        max_order_notional,
                    )
                    cash_delta -= outlay
                    scale_into_position(positions[sym], fill, cost_model)
                    pos = positions[sym]
                    # WHY: re-issuing an add with a new stop/target lets a
                    # strategy trail its stop without a separate action type.
                    if action.stop_price is not None:
                        pos.stop_price = action.stop_price
                    if action.take_profit_price is not None:
                        pos.take_profit_price = action.take_profit_price
                    margin_locked, leverage, liq_price, margin_mode = _margin_fields(
                        pos.entry_price, pos.quantity, pos.side, cost_model
                    )
                    events.append(
                        PositionEvent(
                            ts=ts,
                            symbol=sym,
                            side=pos.side,
                            event_type="add",
                            fill_quantity=fill.quantity,
                            price=price,
                            entry_price=pos.entry_price,
                            remaining_quantity=pos.quantity,
                            notional=price * fill.quantity * cost_model.multiplier,
                            commission=fill.commission,
                            slippage=fill.slippage,
                            tax=fill.tax,
                            reason=reason,
                            group_id=group_id,
                            time_in_force=time_in_force,
                            entry_at=pos.entry_at,
                            margin_locked=margin_locked,
                            leverage=leverage,
                            liquidation_price=liq_price,
                            margin_mode=margin_mode,
                            cash_flow=-outlay,
                        )
                    )
                    volume_consumed[sym] = volume_consumed.get(sym, 0.0) + fill.quantity
                    adv_consumed[sym] = adv_consumed.get(sym, 0.0) + fill.quantity
                else:
                    runtime_events.append(_skipped(ts, skip_reason, symbol=sym))

            else:
                # OPPOSITE SIDE — reject
                logger.warning(
                    "Rejected %s %s: already %s — close first",
                    action.action,
                    sym,
                    positions[sym].side,
                )
                runtime_events.append(_skipped(ts, "opposite_side", symbol=sym))

        elif action.action == "close" and sym in positions:
            pos = positions[sym]
            close_qty = action.quantity

            # Reject zero-quantity close
            if close_qty is not None and close_qty <= 0:
                continue
            bar_volume = get_volume(sym) if get_volume else None
            max_volume_qty = _volume_fill_limit(
                sym,
                max_bar_volume_participation_rate,
                bar_volume,
                max_adv_participation_rate=max_adv_participation_rate,
                lagged_adv=get_lagged_adv(sym) if get_lagged_adv else None,
                used_bar_quantity=volume_consumed.get(sym, 0.0),
                used_adv_quantity=adv_consumed.get(sym, 0.0),
            )
            requested_qty = min(close_qty, pos.quantity) if close_qty is not None else pos.quantity
            if max_volume_qty is not None:
                requested_qty = min(requested_qty, max_volume_qty)
            if requested_qty <= EPSILON:
                if max_volume_qty is not None:
                    runtime_events.append(_skipped(ts, "volume_capped", symbol=sym))
                continue

            trade, event, proceeds, fully_closed = build_close_event(
                pos,
                ts,
                price,
                cost_model,
                reason,
                quantity=requested_qty,
                bar_volume=bar_volume,
                time_in_force=time_in_force,
            )
            trades.append(trade)
            events.append(event)
            volume_consumed[sym] = volume_consumed.get(sym, 0.0) + requested_qty
            adv_consumed[sym] = adv_consumed.get(sym, 0.0) + requested_qty
            cash_delta += proceeds

            if fully_closed:
                del positions[sym]
            else:
                reduce_position(pos, trade.quantity)

    return ExecutionResult(
        trades=trades, events=events, cash_delta=cash_delta, runtime_events=runtime_events
    )


def _validate_scale_in_group_identity(
    intents: list[OrderIntent],
    positions: dict[str, PositionState],
    primary_symbol: str,
) -> None:
    """Refuse a same-side add whose group_id differs from the position's.

    One net position carries one group identity through its add, close,
    trade, and financing records (issue #113), so it can only be scaled by
    an intent of that identity -- grouped-to-ungrouped and ungrouped-to-
    grouped included. Runs before any mutation so a violating intent
    anywhere in the batch leaves the book untouched; venue attribution never
    refuses a confirmed fill, which is why this lives at plan time and not
    in apply_execution_fill.
    """
    for intent in intents:
        if intent.action not in ("long", "short"):
            continue
        symbol = intent.symbol or primary_symbol
        position = positions.get(symbol)
        if position is None or position.side != intent.action:
            continue
        if intent.group_id != position.group_id:
            raise ValueError(
                f"cannot scale {symbol} across group identities: "
                f"position={position.group_id!r}, intent={intent.group_id!r}"
            )


def _preflight_intent_group(
    group_id: str,
    members: list[OrderIntent],
    positions: dict[str, PositionState],
    *,
    get_price: Callable[[str, OrderIntent], float | None],
    primary_symbol: str,
) -> tuple[list[str], dict[int, float], list[float]]:
    """Prove every leg can fill as specified, or raise before any mutation.

    Returns (symbols, price by intent id, expected fill per leg). A leg that
    cannot fill as written -- no price, no position to close, or a close
    larger than the position -- fails loudly here: a group is the strategy's
    request for fill-or-kill, and the ADR in
    docs/decisions/2026-08-05-grouped-decisions-no-engine-side-waiting.md
    settled that such a group raises rather than fills asymmetrically or is
    skipped quietly.
    """
    symbols = [intent.symbol or primary_symbol for intent in members]
    prices_by_intent: dict[int, float] = {}
    missing_prices: list[str] = []
    for intent, symbol in zip(members, symbols, strict=True):
        price = get_price(symbol, intent)
        if price is None or price <= 0:
            missing_prices.append(symbol)
        else:
            prices_by_intent[id(intent)] = price
    if missing_prices:
        raise ValueError(
            f"group {group_id!r} lost pricing for {sorted(missing_prices)} between decision "
            "and execution; cannot fill some legs and not others"
        )

    expected_fills: list[float] = []
    for intent, symbol in zip(members, symbols, strict=True):
        if intent.action != "close":
            if intent.quantity is None:
                raise ValueError(
                    f"group {group_id!r} requires explicit quantities on every entry leg"
                )
            expected_fills.append(intent.quantity)
            continue
        position = positions.get(symbol)
        if position is None:
            raise ValueError(f"group {group_id!r} closes {symbol} but no position is open")
        if intent.quantity is None:
            expected_fills.append(position.quantity)
        elif intent.quantity > position.quantity + EPSILON:
            raise ValueError(
                f"group {group_id!r} closes {intent.quantity} {symbol} but only "
                f"{position.quantity} is held; omit the quantity to close the position"
            )
        else:
            expected_fills.append(intent.quantity)
    return symbols, prices_by_intent, expected_fills


def _execute_order_intent_groups_atomically(
    intents: list[OrderIntent],
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    get_price: Callable[[str, OrderIntent], float | None],
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None,
    max_order_notional: float | None,
    max_bar_volume_participation_rate: float | None,
    max_adv_participation_rate: float | None,
    get_volume: Callable[[str], float | None] | None,
    get_lagged_adv: Callable[[str], float | None] | None,
    used_bar_quantity_by_symbol: dict[str, float] | None,
    used_adv_quantity_by_symbol: dict[str, float] | None,
) -> ExecutionResult:
    """Execute grouped simulation intents against isolated staged state."""
    units: list[tuple[str | None, list[OrderIntent]]] = []
    grouped_units: dict[str, list[OrderIntent]] = {}
    for intent in intents:
        group_id = intent.group_id
        if group_id is None:
            units.append((None, [intent]))
            continue
        if group_id not in grouped_units:
            grouped_units[group_id] = []
            units.append((group_id, grouped_units[group_id]))
        grouped_units[group_id].append(intent)

    trades: list[TradeResult] = []
    events: list[PositionEvent] = []
    runtime_events: list[RuntimeEvent] = []
    cash_delta = 0.0
    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}

    common_kwargs = {
        "get_cost_model": get_cost_model,
        "primary_symbol": primary_symbol,
        "max_position_notional": max_position_notional,
        "max_order_notional": max_order_notional,
        "max_bar_volume_participation_rate": max_bar_volume_participation_rate,
        "max_adv_participation_rate": max_adv_participation_rate,
        "get_volume": get_volume,
        "get_lagged_adv": get_lagged_adv,
    }

    # WHY: ungrouped units commit straight into the caller's book as the loop
    # runs, so every group is proven fillable-as-specified before the first
    # unit executes. A raise later would leave positions moved while the cash
    # delta is discarded with the exception. Reading position sizes here is
    # sound because a decision holds at most one intent per symbol, so no
    # earlier unit can resize a symbol a later group closes.
    preflight = {
        group_id: _preflight_intent_group(
            group_id,
            members,
            positions,
            get_price=get_price,
            primary_symbol=primary_symbol,
        )
        for group_id, members in units
        if group_id is not None
    }

    for group_id, members in units:
        available_cash = cash + cash_delta
        if group_id is None:
            result = execute_order_intents(
                members,
                positions,
                available_cash,
                ts,
                get_price=get_price,
                used_bar_quantity_by_symbol=volume_consumed,
                used_adv_quantity_by_symbol=adv_consumed,
                **common_kwargs,
            )
            trades.extend(result.trades)
            events.extend(result.events)
            runtime_events.extend(result.runtime_events)
            cash_delta += result.cash_delta
            continue

        symbols, prices_by_intent, expected_fills = preflight[group_id]
        staged_positions = deepcopy(positions)
        staged_volume_consumed = dict(volume_consumed)
        staged_adv_consumed = dict(adv_consumed)
        result = execute_order_intents(
            members,
            staged_positions,
            available_cash,
            ts,
            get_price=lambda _symbol, intent, _prices=prices_by_intent: _prices[id(intent)],
            used_bar_quantity_by_symbol=staged_volume_consumed,
            used_adv_quantity_by_symbol=staged_adv_consumed,
            **common_kwargs,
        )
        # Preflight settled that each leg can fill as written; what remains
        # is whether the venue let it -- cash, bar volume, and ADV budgets.
        fully_filled = len(result.events) == len(members) and all(
            event.symbol == symbol
            and np.isclose(event.fill_quantity, expected, rtol=0.0, atol=EPSILON)
            for symbol, expected, event in zip(symbols, expected_fills, result.events, strict=True)
        )
        if not fully_filled:
            failed_reasons = sorted(
                {str(event.detail.get("reason", "unfilled")) for event in result.runtime_events}
                or {"partial_fill"}
            )
            runtime_events.append(
                _skipped(
                    ts,
                    "group_unfillable",
                    group_id=group_id,
                    symbols=symbols,
                    failed_reasons=failed_reasons,
                )
            )
            continue

        positions.clear()
        positions.update(staged_positions)
        volume_consumed.clear()
        volume_consumed.update(staged_volume_consumed)
        adv_consumed.clear()
        adv_consumed.update(staged_adv_consumed)
        trades.extend(result.trades)
        events.extend(result.events)
        runtime_events.extend(result.runtime_events)
        cash_delta += result.cash_delta

    return ExecutionResult(
        trades=trades,
        events=events,
        cash_delta=cash_delta,
        runtime_events=runtime_events,
    )


def _scale_additions_to_cash(
    actions: list[OrderIntent],
    available_cash: float,
    ts: datetime,
    *,
    prices: dict[str, float],
    get_cost_model: Callable[[str], CostModel],
    get_volume: Callable[[str], float | None] | None = None,
) -> tuple[list[OrderIntent], list[RuntimeEvent]]:
    """Scale all rebalance additions by one factor when cash is insufficient.

    A common factor preserves the intended cross-sectional allocation better
    than accepting actions in symbol order until cash runs out. The binary
    search accounts for nonlinear minimum commissions.
    """
    if not actions:
        return [], []

    if available_cash <= EPSILON:
        logger.warning("Rebalance additions skipped: insufficient cash after reductions")
        event = _skipped(
            ts,
            "insufficient_cash",
            symbols=sorted({action.symbol for action in actions}),
            available_cash=available_cash,
        )
        return [], [event]

    def total_outlay(scale: float) -> float:
        total = 0.0
        for action in actions:
            quantity = (action.quantity or 0.0) * scale
            if quantity <= EPSILON:
                continue
            price = prices[action.symbol]
            cost_model = get_cost_model(action.symbol)
            side: PositionSide = "short" if action.action == "short" else "long"
            total += cost_model.estimate_entry_outlay(
                price,
                quantity,
                side,
                bar_volume=get_volume(action.symbol) if get_volume else None,
            )
        return total

    if total_outlay(1.0) <= available_cash + EPSILON:
        return actions, []

    low, high = 0.0, 1.0
    for _ in range(60):
        middle = (low + high) / 2.0
        if total_outlay(middle) <= available_cash:
            low = middle
        else:
            high = middle

    if low <= EPSILON:
        logger.warning("Rebalance additions skipped: insufficient cash after reductions")
        event = _skipped(
            ts,
            "insufficient_cash",
            symbols=sorted({action.symbol for action in actions}),
            available_cash=available_cash,
        )
        return [], [event]

    logger.info("Rebalance additions scaled to %.6f of requested quantities", low)
    return [
        OrderIntent(
            action=action.action,
            symbol=action.symbol,
            quantity=(action.quantity or 0.0) * low,
            reason=action.reason,
            limit_price=action.limit_price,
        )
        for action in actions
    ], []


def _orders_for_target_notional(
    symbol: str,
    target_signed_notional: float,
    reason: str,
    positions: dict[str, PositionState],
    price: float,
    *,
    get_cost_model: Callable[[str], CostModel],
) -> list[RebalanceOrderState]:
    """Resolve one fixed target notional into quantities at a fresh price."""
    position = positions.get(symbol)
    current_signed_quantity = (
        position.quantity * side_multiplier(position.side) if position is not None else 0.0
    )
    target_signed_quantity = target_signed_notional / (price * get_cost_model(symbol).multiplier)

    reductions: list[OrderIntent] = []
    additions: list[OrderIntent] = []
    current_is_flat = abs(current_signed_quantity) <= EPSILON
    target_is_flat = abs(target_signed_quantity) <= EPSILON
    same_direction = (
        current_is_flat
        or target_is_flat
        or (current_signed_quantity > 0) == (target_signed_quantity > 0)
    )
    if same_direction:
        quantity_delta = abs(target_signed_quantity) - abs(current_signed_quantity)
        if quantity_delta < -EPSILON:
            reductions.append(
                OrderIntent(
                    action="close",
                    symbol=symbol,
                    quantity=-quantity_delta,
                    reason=reason,
                )
            )
        elif quantity_delta > EPSILON:
            additions.append(
                OrderIntent(
                    action="long" if target_signed_quantity > 0 else "short",
                    symbol=symbol,
                    quantity=quantity_delta,
                    reason=reason,
                )
            )
    else:
        reductions.append(
            OrderIntent(
                action="close",
                symbol=symbol,
                quantity=position.quantity if position is not None else None,
                reason=reason,
            )
        )
        additions.append(
            OrderIntent(
                action="long" if target_signed_quantity > 0 else "short",
                symbol=symbol,
                quantity=abs(target_signed_quantity),
                reason=reason,
            )
        )

    return [
        RebalanceOrderState(
            intent=action,
            phase=phase,
            requested_quantity=action.quantity or 0.0,
            remaining_quantity=action.quantity or 0.0,
        )
        for phase, actions in (("reduction", reductions), ("addition", additions))
        for action in actions
    ]


def _plan_portfolio_weights(
    targets: PortfolioWeights,
    positions: dict[str, PositionState],
    cash: float,
    *,
    get_reference_price: Callable[[str], float | None],
    get_cost_model: Callable[[str], CostModel],
    get_fresh_price: Callable[[str], float | None] | None = None,
) -> tuple[PortfolioRebalanceState, dict[str, float]]:
    """Freeze target notionals and resolve quantities only from fresh prices."""
    target_symbols = {symbol for symbol, weight in targets.weights.items() if abs(weight) > EPSILON}
    relevant_symbols = sorted(set(positions) | target_symbols)
    if not relevant_symbols:
        return PortfolioRebalanceState(target=targets, orders=()), {}

    valuation_prices: dict[str, float] = {}
    unavailable_positions: list[str] = []
    for symbol in positions:
        raw_price = get_reference_price(symbol)
        if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
            unavailable_positions.append(symbol)
        else:
            valuation_prices[symbol] = float(raw_price)
    if unavailable_positions:
        raise ExecutionPriceUnavailableError(unavailable_positions)

    equity, _ = calc_equity(
        cash,
        positions,
        get_price=lambda symbol, _position: valuation_prices[symbol],
        get_cost_model=get_cost_model,
    )
    if equity <= EPSILON:
        raise ValueError("rebalance requires positive execution-time equity")

    fresh_price = get_fresh_price or get_reference_price
    prices: dict[str, float] = {}
    orders: list[RebalanceOrderState] = []
    unresolved_legs: list[UnresolvedRebalanceLeg] = []
    for symbol in relevant_symbols:
        target_signed_notional = targets.weights.get(symbol, 0.0) * equity
        raw_price = fresh_price(symbol)
        if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
            if abs(target_signed_notional) <= EPSILON:
                position = positions.get(symbol)
                if position is not None:
                    orders.extend(
                        _orders_for_target_notional(
                            symbol,
                            target_signed_notional,
                            targets.reason,
                            positions,
                            position.entry_price,
                            get_cost_model=get_cost_model,
                        )
                    )
            else:
                unresolved_legs.append(
                    UnresolvedRebalanceLeg(
                        symbol=symbol,
                        target_signed_notional=target_signed_notional,
                        reason=targets.reason,
                    )
                )
            continue
        price = float(raw_price)
        prices[symbol] = price
        orders.extend(
            _orders_for_target_notional(
                symbol,
                target_signed_notional,
                targets.reason,
                positions,
                price,
                get_cost_model=get_cost_model,
            )
        )
    return (
        PortfolioRebalanceState(
            target=targets,
            orders=tuple(orders),
            unresolved_legs=tuple(unresolved_legs),
        ),
        prices,
    )


def execute_portfolio_weights(
    targets: PortfolioWeights,
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    get_price: Callable[[str, OrderIntent], float | None],
    get_reference_price: Callable[[str], float | None] | None = None,
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_order_notional: float | None = None,
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
) -> ExecutionResult:
    """Resolve and execute a portfolio rebalance as one deterministic batch."""
    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}
    reference_price = get_reference_price or (
        lambda symbol: get_price(
            symbol,
            OrderIntent(
                action=(
                    "long"
                    if targets.weights.get(symbol, 0.0) > EPSILON
                    else "short"
                    if targets.weights.get(symbol, 0.0) < -EPSILON
                    else "close"
                ),
                symbol=symbol,
                reason=targets.reason,
            ),
        )
    )
    state, prices = _plan_portfolio_weights(
        targets,
        positions,
        cash,
        get_reference_price=reference_price,
        get_cost_model=get_cost_model,
    )
    if state.unresolved_legs:
        raise ExecutionPriceUnavailableError([leg.symbol for leg in state.unresolved_legs])
    reductions = [order.intent for order in state.orders if order.phase == "reduction"]
    additions = [order.intent for order in state.orders if order.phase == "addition"]

    # WHY: a whole-book target is ungrouped by nature, so adding to a position
    # a group opened would break the one-position-one-group invariant (issue
    # #113). Refuse before the reductions this target would otherwise execute
    # first, so the book is untouched. A symbol planned in both phases is a
    # flip, not a scale-in: its reduction carries the position's own group_id
    # out before the addition opens a fresh one.
    flipped = {intent.symbol for intent in reductions} & {intent.symbol for intent in additions}
    grouped_scale_ins = sorted(
        {
            intent.symbol
            for intent in additions
            if intent.symbol not in flipped
            and (position := positions.get(intent.symbol)) is not None
            and position.group_id is not None
        }
    )
    if grouped_scale_ins:
        raise ValueError(
            f"PortfolioWeights cannot scale {grouped_scale_ins}: held under a "
            "group id; close the group first or manage the symbol with grouped intents"
        )

    unavailable_actions = sorted(
        {
            action.symbol
            for action in [*reductions, *additions]
            if (price := get_price(action.symbol, action)) is None
            or not isfinite(price)
            or price <= 0
        }
    )
    if unavailable_actions:
        raise ExecutionPriceUnavailableError(unavailable_actions)

    reduction_result = execute_order_intents(
        reductions,
        positions,
        cash,
        ts,
        get_price=get_price,
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        max_adv_participation_rate=max_adv_participation_rate,
        get_volume=get_volume,
        get_lagged_adv=get_lagged_adv,
        used_bar_quantity_by_symbol=volume_consumed,
        used_adv_quantity_by_symbol=adv_consumed,
    )
    cash_after_reductions = cash + reduction_result.cash_delta
    scaled_additions, cash_events = _scale_additions_to_cash(
        additions,
        cash_after_reductions,
        ts,
        prices=prices,
        get_cost_model=get_cost_model,
        get_volume=get_volume,
    )
    addition_result = execute_order_intents(
        scaled_additions,
        positions,
        cash_after_reductions,
        ts,
        get_price=get_price,
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
        max_position_notional=max_position_notional,
        max_order_notional=max_order_notional,
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        max_adv_participation_rate=max_adv_participation_rate,
        get_volume=get_volume,
        get_lagged_adv=get_lagged_adv,
        used_bar_quantity_by_symbol=volume_consumed,
        used_adv_quantity_by_symbol=adv_consumed,
    )
    return ExecutionResult(
        trades=[*reduction_result.trades, *addition_result.trades],
        events=[*reduction_result.events, *addition_result.events],
        cash_delta=reduction_result.cash_delta + addition_result.cash_delta,
        runtime_events=[
            *reduction_result.runtime_events,
            *cash_events,
            *addition_result.runtime_events,
        ],
    )


def _rebalance_residual_event(
    ts: datetime,
    order: RebalanceOrderState,
    *,
    requested_quantity: float,
    filled_quantity: float,
    remaining_quantity: float,
    blocked_symbols: list[str] | None = None,
) -> RuntimeEvent:
    detail: dict[str, object] = {
        "phase": order.phase,
        "action": order.intent.action,
        "quantity_scope": "attempt",
        "requested_quantity": requested_quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": remaining_quantity,
    }
    if blocked_symbols:
        detail["blocked_symbols"] = blocked_symbols
    return _skipped(ts, "rebalance_residual", symbol=order.intent.symbol, **detail)


def _unresolved_rebalance_event(
    ts: datetime,
    leg: UnresolvedRebalanceLeg,
    *,
    blocked_symbols: list[str] | None = None,
) -> RuntimeEvent:
    target_notional = abs(leg.target_signed_notional)
    detail: dict[str, object] = {
        "sizing_state": "awaiting_fresh_price",
        "notional_scope": "target_allocation",
        "target_side": "long" if leg.target_signed_notional > 0 else "short",
        "requested_notional": target_notional,
        "filled_notional": 0.0,
        "remaining_notional": target_notional,
    }
    if blocked_symbols:
        detail["blocked_symbols"] = blocked_symbols
    return _skipped(ts, "rebalance_residual", symbol=leg.symbol, **detail)


def _rebalance_quantity_events(
    ts: datetime,
    records: list[tuple[RebalanceOrderState, float, float, float]],
    *,
    blocked_symbols: list[str] | None = None,
    report_all_blocked: bool = False,
) -> list[RuntimeEvent]:
    """Emit at most one durable residual event per timestamp and symbol."""
    records_by_symbol: dict[str, list[tuple[RebalanceOrderState, float, float, float]]] = {}
    for record in records:
        records_by_symbol.setdefault(record[0].intent.symbol, []).append(record)

    events: list[RuntimeEvent] = []
    for symbol, symbol_records in records_by_symbol.items():
        if len(symbol_records) == 1:
            order, requested, filled, remaining = symbol_records[0]
            events.append(
                _rebalance_residual_event(
                    ts,
                    order,
                    requested_quantity=requested,
                    filled_quantity=filled,
                    remaining_quantity=remaining,
                    blocked_symbols=(
                        blocked_symbols
                        if blocked_symbols and (report_all_blocked or symbol in blocked_symbols)
                        else None
                    ),
                )
            )
            continue

        phases = [
            {
                "phase": order.phase,
                "action": order.intent.action,
                "requested_quantity": requested,
                "filled_quantity": filled,
                "remaining_quantity": remaining,
            }
            for order, requested, filled, remaining in symbol_records
        ]
        detail: dict[str, object] = {
            "reason": "rebalance_residual",
            "quantity_scope": "attempt",
            "requested_quantity": sum(record[1] for record in symbol_records),
            "filled_quantity": sum(record[2] for record in symbol_records),
            "remaining_quantity": sum(record[3] for record in symbol_records),
            "phases": phases,
        }
        if blocked_symbols and (report_all_blocked or symbol in blocked_symbols):
            detail["blocked_symbols"] = blocked_symbols
        events.append(
            RuntimeEvent(
                ts=ts,
                event_type="decision_skipped",
                symbol=symbol,
                detail=detail,
            )
        )
    return events


def _cancel_rebalance_symbols(
    state: PortfolioRebalanceState,
    exit_reasons: Mapping[str, str],
    ts: datetime,
) -> tuple[PortfolioRebalanceState | None, list[RuntimeEvent]]:
    """Cancel target legs owned by a higher-priority protective exit."""
    cancelled_orders: dict[str, list[RebalanceOrderState]] = {}
    retained_orders: list[RebalanceOrderState] = []
    for order in state.orders:
        symbol = order.intent.symbol
        if symbol in exit_reasons:
            cancelled_orders.setdefault(symbol, []).append(order)
        else:
            retained_orders.append(order)

    cancelled_legs: dict[str, UnresolvedRebalanceLeg] = {}
    retained_legs: list[UnresolvedRebalanceLeg] = []
    for leg in state.unresolved_legs:
        if leg.symbol in exit_reasons:
            cancelled_legs[leg.symbol] = leg
        else:
            retained_legs.append(leg)

    events: list[RuntimeEvent] = []
    for symbol in sorted(set(cancelled_orders) | set(cancelled_legs)):
        phases = [
            {
                "phase": order.phase,
                "action": order.intent.action,
                "requested_quantity": order.requested_quantity,
                "filled_quantity": order.requested_quantity - order.remaining_quantity,
                "cancelled_quantity": order.remaining_quantity,
            }
            for order in cancelled_orders.get(symbol, [])
        ]
        detail: dict[str, object] = {
            "exit_reason": exit_reasons[symbol],
            "phases": phases,
        }
        leg = cancelled_legs.get(symbol)
        if leg is not None:
            detail.update(
                {
                    "sizing_state": "cancelled_before_quantity_resolution",
                    "target_notional": abs(leg.target_signed_notional),
                }
            )
        events.append(
            _skipped(
                ts,
                "rebalance_cancelled_by_protective_exit",
                symbol=symbol,
                **detail,
            )
        )

    next_state = (
        PortfolioRebalanceState(
            target=state.target,
            orders=tuple(retained_orders),
            unresolved_legs=tuple(retained_legs),
        )
        if retained_orders or retained_legs
        else None
    )
    return next_state, events


def execute_portfolio_rebalance_slice(
    state: PortfolioRebalanceState,
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    residual_policy: RebalanceResidualPolicy,
    get_price: Callable[[str, OrderIntent], float | None],
    get_fresh_price: Callable[[str], float | None],
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_order_notional: float | None = None,
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
) -> ExecutionResult:
    """Execute one bounded-liquidity slice and retain only true residuals."""
    if residual_policy == "discard":
        raise ValueError("discard policy cannot retain a cross-bar rebalance state")

    pending_exit_reasons = {
        symbol: position.pending_market_exit_reason
        for symbol, position in positions.items()
        if position.pending_market_exit_reason is not None
    }
    cancellation_events: list[RuntimeEvent] = []
    if pending_exit_reasons:
        state_symbols = {order.intent.symbol for order in state.orders} | {
            leg.symbol for leg in state.unresolved_legs
        }
        conflicts = sorted(state_symbols & set(pending_exit_reasons))
        if residual_policy == "fail" and conflicts:
            raise ValueError(
                "PortfolioWeights fail policy conflicts with pending protective exits for "
                f"{conflicts}"
            )
        retained_state, cancellation_events = _cancel_rebalance_symbols(
            state,
            pending_exit_reasons,
            ts,
        )
        if retained_state is None:
            return ExecutionResult(
                trades=[],
                events=[],
                cash_delta=0.0,
                runtime_events=cancellation_events,
            )
        state = retained_state

    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}
    resolved_orders = list(state.orders)
    unresolved_legs: list[UnresolvedRebalanceLeg] = []
    for leg in state.unresolved_legs:
        raw_price = get_fresh_price(leg.symbol)
        if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
            unresolved_legs.append(leg)
            continue
        resolved_orders.extend(
            _orders_for_target_notional(
                leg.symbol,
                leg.target_signed_notional,
                leg.reason,
                positions,
                float(raw_price),
                get_cost_model=get_cost_model,
            )
        )

    pending_orders: list[RebalanceOrderState] = []
    for order in resolved_orders:
        remaining_quantity = order.remaining_quantity
        if order.phase == "reduction":
            position = positions.get(order.intent.symbol)
            remaining_quantity = (
                min(remaining_quantity, position.quantity) if position is not None else 0.0
            )
        if remaining_quantity > EPSILON:
            pending_orders.append(replace(order, remaining_quantity=remaining_quantity))
    priced_intents: dict[tuple[RebalancePhase, str], tuple[OrderIntent, float | None]] = {}
    blocked_symbols = {leg.symbol for leg in unresolved_legs}
    for order in pending_orders:
        symbol = order.intent.symbol
        intent = replace(order.intent, quantity=order.remaining_quantity)
        raw_price = get_price(symbol, intent)
        price = (
            float(raw_price)
            if raw_price is not None and isfinite(raw_price) and raw_price > 0
            else None
        )
        priced_intents[(order.phase, symbol)] = (intent, price)
        max_volume_qty = _volume_fill_limit(
            symbol,
            max_bar_volume_participation_rate,
            get_volume(symbol) if get_volume else None,
            max_adv_participation_rate=max_adv_participation_rate,
            lagged_adv=get_lagged_adv(symbol) if get_lagged_adv else None,
            used_bar_quantity=volume_consumed.get(symbol, 0.0),
            used_adv_quantity=adv_consumed.get(symbol, 0.0),
        )
        if price is None or (max_volume_qty is not None and max_volume_qty <= EPSILON):
            blocked_symbols.add(symbol)

    if residual_policy == "defer_all" and blocked_symbols:
        blocked = sorted(blocked_symbols)
        runtime_events = [
            *cancellation_events,
            *_rebalance_quantity_events(
                ts,
                [
                    (order, order.remaining_quantity, 0.0, order.remaining_quantity)
                    for order in pending_orders
                ],
                blocked_symbols=blocked,
                report_all_blocked=True,
            ),
        ]
        runtime_events.extend(
            _unresolved_rebalance_event(ts, leg, blocked_symbols=blocked) for leg in unresolved_legs
        )
        return ExecutionResult(
            trades=[],
            events=[],
            cash_delta=0.0,
            runtime_events=runtime_events,
            pending_rebalance=PortfolioRebalanceState(
                target=state.target,
                orders=tuple(pending_orders),
                unresolved_legs=tuple(unresolved_legs),
            ),
        )

    remaining_by_key = {
        (order.phase, order.intent.symbol): order.remaining_quantity for order in pending_orders
    }
    requested_by_key = {
        (order.phase, order.intent.symbol): order.requested_quantity for order in pending_orders
    }
    attempted_by_key: dict[tuple[RebalancePhase, str], float] = {}
    filled_by_key: dict[tuple[RebalancePhase, str], float] = {}
    blocked = sorted(blocked_symbols)

    reduction_intents: list[OrderIntent] = []
    reduction_prices: dict[str, float] = {}
    for order in pending_orders:
        if order.phase != "reduction" or order.intent.symbol in blocked_symbols:
            continue
        key = (order.phase, order.intent.symbol)
        reconciled_quantity = order.remaining_quantity
        _, price = priced_intents[key]
        assert price is not None
        reduction_intents.append(replace(order.intent, quantity=reconciled_quantity))
        reduction_prices[order.intent.symbol] = price
        attempted_by_key[key] = reconciled_quantity

    reduction_result = execute_order_intents(
        reduction_intents,
        positions,
        cash,
        ts,
        get_price=lambda symbol, _action: reduction_prices.get(symbol),
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        max_adv_participation_rate=max_adv_participation_rate,
        get_volume=get_volume,
        get_lagged_adv=get_lagged_adv,
        used_bar_quantity_by_symbol=volume_consumed,
        used_adv_quantity_by_symbol=adv_consumed,
    )
    for event in reduction_result.events:
        key = ("reduction", event.symbol)
        filled_by_key[key] = filled_by_key.get(key, 0.0) + event.fill_quantity
        remaining_by_key[key] = max(remaining_by_key[key] - event.fill_quantity, 0.0)

    cash_after_reductions = cash + reduction_result.cash_delta
    addition_intents: list[OrderIntent] = []
    addition_prices: dict[str, float] = {}
    position_limit_cancellations: dict[tuple[RebalancePhase, str], tuple[float, float]] = {}
    for order in pending_orders:
        if order.phase != "addition" or order.intent.symbol in blocked_symbols:
            continue
        key = (order.phase, order.intent.symbol)
        reduction_remaining = remaining_by_key.get(("reduction", order.intent.symbol), 0.0)
        if reduction_remaining > EPSILON:
            continue
        _, price = priced_intents[key]
        assert price is not None
        quantity = order.remaining_quantity
        if max_position_notional is not None:
            position = positions.get(order.intent.symbol)
            existing_quantity = position.quantity if position is not None else 0.0
            unit_notional = price * get_cost_model(order.intent.symbol).multiplier
            available_quantity = max(max_position_notional / unit_notional - existing_quantity, 0.0)
            if quantity > available_quantity + EPSILON:
                if residual_policy == "fail":
                    raise ValueError(
                        "PortfolioWeights target exceeds max_position_notional for "
                        f"{order.intent.symbol!r}"
                    )
                position_limit_cancellations[key] = (
                    quantity,
                    quantity - available_quantity,
                )
                requested_by_key[key] -= quantity - available_quantity
                quantity = available_quantity
                remaining_by_key[key] = quantity
        if quantity <= EPSILON:
            continue
        addition_intents.append(replace(order.intent, quantity=quantity))
        addition_prices[order.intent.symbol] = price
        attempted_by_key[key] = quantity

    scaled_additions, cash_events = _scale_additions_to_cash(
        addition_intents,
        cash_after_reductions,
        ts,
        prices=addition_prices,
        get_cost_model=get_cost_model,
        get_volume=get_volume,
    )
    has_future_reduction = any(
        remaining_by_key.get(("reduction", order.intent.symbol), 0.0) > EPSILON
        for order in pending_orders
        if order.phase == "reduction"
    ) or any(leg.symbol in positions for leg in unresolved_legs)
    cash_limit_cancellations: dict[tuple[RebalancePhase, str], tuple[float, float]] = {}
    if not has_future_reduction:
        scaled_quantity_by_symbol = {
            intent.symbol: intent.quantity or 0.0 for intent in scaled_additions
        }
        for intent in addition_intents:
            key = ("addition", intent.symbol)
            final_quantity = scaled_quantity_by_symbol.get(intent.symbol, 0.0)
            cancelled_quantity = remaining_by_key[key] - final_quantity
            if cancelled_quantity > EPSILON:
                cash_limit_cancellations[key] = (
                    remaining_by_key[key],
                    cancelled_quantity,
                )
            requested_by_key[key] -= cancelled_quantity
            remaining_by_key[key] = final_quantity
            attempted_by_key[key] = final_quantity
    addition_result = execute_order_intents(
        scaled_additions,
        positions,
        cash_after_reductions,
        ts,
        get_price=lambda symbol, _action: addition_prices.get(symbol),
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
        max_position_notional=max_position_notional,
        max_order_notional=max_order_notional,
        max_bar_volume_participation_rate=max_bar_volume_participation_rate,
        max_adv_participation_rate=max_adv_participation_rate,
        get_volume=get_volume,
        get_lagged_adv=get_lagged_adv,
        used_bar_quantity_by_symbol=volume_consumed,
        used_adv_quantity_by_symbol=adv_consumed,
    )
    for event in addition_result.events:
        key = ("addition", event.symbol)
        filled_by_key[key] = filled_by_key.get(key, 0.0) + event.fill_quantity
        remaining_by_key[key] = max(remaining_by_key[key] - event.fill_quantity, 0.0)

    updated_orders = tuple(
        replace(
            order,
            requested_quantity=requested_by_key[(order.phase, order.intent.symbol)],
            remaining_quantity=remaining_by_key[(order.phase, order.intent.symbol)],
        )
        for order in pending_orders
        if remaining_by_key[(order.phase, order.intent.symbol)] > EPSILON
    )
    runtime_events = [
        *cancellation_events,
        *reduction_result.runtime_events,
        *cash_events,
        *addition_result.runtime_events,
    ]
    for key, (requested_quantity, cancelled_quantity) in position_limit_cancellations.items():
        _, symbol = key
        runtime_events.append(
            _skipped(
                ts,
                "rebalance_constrained_by_position_limit",
                symbol=symbol,
                phase="addition",
                action=next(
                    order.intent.action
                    for order in pending_orders
                    if (order.phase, order.intent.symbol) == key
                ),
                quantity_scope="target",
                requested_quantity=requested_quantity,
                filled_quantity=filled_by_key.get(key, 0.0),
                cancelled_quantity=cancelled_quantity,
                remaining_quantity=remaining_by_key[key],
            )
        )
    for key, (requested_quantity, cancelled_quantity) in cash_limit_cancellations.items():
        _, symbol = key
        runtime_events.append(
            _skipped(
                ts,
                "rebalance_constrained_by_cash",
                symbol=symbol,
                phase="addition",
                action=next(
                    order.intent.action
                    for order in pending_orders
                    if (order.phase, order.intent.symbol) == key
                ),
                quantity_scope="target",
                requested_quantity=requested_quantity,
                filled_quantity=filled_by_key.get(key, 0.0),
                cancelled_quantity=cancelled_quantity,
                remaining_quantity=remaining_by_key[key],
            )
        )
    residual_records: list[tuple[RebalanceOrderState, float, float, float]] = []
    for order in pending_orders:
        key = (order.phase, order.intent.symbol)
        remaining_quantity = remaining_by_key[key]
        if remaining_quantity <= EPSILON:
            continue
        residual_records.append(
            (
                order,
                attempted_by_key.get(key, order.remaining_quantity),
                filled_by_key.get(key, 0.0),
                remaining_quantity,
            )
        )
    runtime_events.extend(_rebalance_quantity_events(ts, residual_records, blocked_symbols=blocked))
    runtime_events.extend(_unresolved_rebalance_event(ts, leg) for leg in unresolved_legs)

    next_state = (
        PortfolioRebalanceState(
            target=state.target,
            orders=updated_orders,
            unresolved_legs=tuple(unresolved_legs),
        )
        if updated_orders or unresolved_legs
        else None
    )
    return ExecutionResult(
        trades=[*reduction_result.trades, *addition_result.trades],
        events=[*reduction_result.events, *addition_result.events],
        cash_delta=reduction_result.cash_delta + addition_result.cash_delta,
        runtime_events=runtime_events,
        pending_rebalance=next_state,
    )


# ---------------------------------------------------------------------------
# Combined pending-fill + stop-check step (backtest/sim)
# ---------------------------------------------------------------------------


def validate_strategy_decision(
    decision: StrategyDecision,
    universe: set[str],
    *,
    primary_symbol: str,
    bars: dict[str, dict[str, float]],
    positions: dict[str, PositionState],
) -> None:
    """Validate one strategy return value before it enters engine state.

    PortfolioWeights must be immediately executable: every symbol it needs
    must already have a bar this period. OrderIntents sharing a group_id form
    an atomic related-order group with the same requirement — the engine
    does not wait across periods for a grouped decision's data to arrive;
    strategies check readiness themselves via ctx.available_symbols before
    returning one (see examples/multi_leg_spread/strategy.py). Ungrouped
    intents may wait for their own symbol's next bar.
    """
    if isinstance(decision, PortfolioWeights):
        symbols = set(decision.weights)
        target_symbols = {
            symbol for symbol, weight in decision.weights.items() if abs(weight) > EPSILON
        }
        missing = (set(positions) | target_symbols) - set(bars)
        if missing:
            raise ValueError(
                f"PortfolioWeights requires a bar for {sorted(missing)} this period; "
                "check ctx.available_symbols before returning PortfolioWeights"
            )
    else:
        if not isinstance(decision, list):
            raise TypeError("strategy decision must be list[OrderIntent] or PortfolioWeights")
        invalid = [
            type(intent).__name__ for intent in decision if not isinstance(intent, OrderIntent)
        ]
        if invalid:
            raise TypeError(f"strategy decision contains non-OrderIntent values: {invalid}")
        resolved_symbols = [intent.symbol or primary_symbol for intent in decision]
        if len(resolved_symbols) != len(set(resolved_symbols)):
            raise ValueError("strategy decision must contain at most one intent per symbol")
        symbols = set(resolved_symbols)

        groups: dict[str, list[OrderIntent]] = {}
        for intent in decision:
            if intent.group_id is not None:
                groups.setdefault(intent.group_id, []).append(intent)
        for group_id, members in groups.items():
            # WHY: an entry with no quantity sizes from available cash, which
            # is not deterministic across legs. A close with no quantity means
            # the whole position, which is -- and it is the only way to exit a
            # leg whose size changed underneath the strategy.
            if any(member.quantity is None and member.action != "close" for member in members):
                raise ValueError(
                    f"group {group_id!r} requires explicit quantities on every entry leg"
                )
            group_symbols = {member.symbol or primary_symbol for member in members}
            missing = group_symbols - set(bars)
            if missing:
                raise ValueError(
                    f"group {group_id!r} requires a bar for {sorted(missing)} this period; "
                    "check ctx.available_symbols before returning a grouped decision"
                )
    unknown = symbols - universe
    if unknown:
        raise ValueError(f"strategy decision contains unknown symbols: {sorted(unknown)}")


def _intent_executes_at_open(
    intent: OrderIntent,
    bar: dict[str, float],
    default_fill: str,
) -> bool:
    """Return whether an entry is causally known to exist from bar open."""
    if intent.limit_price is None:
        return default_fill == "open"

    open_raw = bar.get("open")
    if open_raw is None:
        return False
    open_price = float(open_raw)
    limit_price = intent.limit_price
    if intent.action == "long":
        return open_price <= limit_price
    if intent.action == "short":
        return open_price >= limit_price
    return False


def partition_pending_decision(
    decision: StrategyDecision,
    bars: dict[str, dict[str, float]],
    positions: dict[str, PositionState],
    *,
    primary_symbol: str,
) -> tuple[StrategyDecision, StrategyDecision]:
    """Split a decision into executable-now and waiting-for-data parts.

    Per-symbol order intents become eligible on that symbol's next observed
    bar and can wait indefinitely without blocking anything else. PortfolioWeights
    and grouped OrderIntents (group_id is not None) are always returned fully
    ready: validate_strategy_decision already required every symbol they need
    to have a bar one period earlier, at decision time — grouped intents can
    never end up waiting.
    """
    if isinstance(decision, PortfolioWeights):
        return decision, []

    ready: list[OrderIntent] = []
    waiting: list[OrderIntent] = []
    for intent in decision:
        symbol = intent.symbol or primary_symbol
        if intent.group_id is not None or symbol in bars:
            ready.append(intent)
        else:
            waiting.append(intent)
    return ready, waiting


def merge_pending_decisions(
    pending: StrategyDecision,
    new_decision: StrategyDecision,
    *,
    primary_symbol: str,
) -> StrategyDecision:
    """Merge a new decision without replacing unresolved engine state.

    Pending values are normally independent per-symbol intents. A backtest
    may also retain ``PortfolioWeights`` while its bounded whole-book
    execution delay is active. A newer complete target supersedes the older
    unfilled target; mixing a portfolio target with symbol-level intents is
    rejected because there is no unambiguous merge rule.
    """
    if not pending:
        return new_decision
    if not new_decision:
        return pending
    if isinstance(pending, PortfolioWeights):
        if isinstance(new_decision, PortfolioWeights):
            return new_decision
        raise ValueError("cannot emit OrderIntents while a PortfolioWeights rebalance is deferred")
    if isinstance(new_decision, PortfolioWeights):
        raise ValueError(
            "cannot return PortfolioWeights while per-symbol intents are still pending"
        )

    pending_intents = list(pending)
    new_intents = list(new_decision)

    pending_symbols = {intent.symbol or primary_symbol for intent in pending_intents}
    new_symbols = {intent.symbol or primary_symbol for intent in new_intents}
    overlap = pending_symbols & new_symbols
    if overlap:
        raise ValueError(f"strategy emitted duplicate pending intents for {sorted(overlap)}")
    return [*pending_intents, *new_intents]


def _validate_no_ambiguous_stop_conflicts(
    pending_decision: StrategyDecision,
    positions: dict[str, PositionState],
    bars: dict[str, dict[str, float]],
    *,
    get_cost_model: Callable[[str], CostModel],
    default_fill: str,
    primary_symbol: str,
    rebalance_state: PortfolioRebalanceState | None = None,
) -> None:
    """Refuse to guess the order of a non-open fill and a triggered protection.

    OHLCV bars cannot establish whether an intrabar stop/target occurred before
    or after a close/high/low fill. Detect it before execution so no cash,
    position, or liquidity-budget mutation can leak from the batch, and raise
    it as a deferrable condition: moving the fill to a later bar leaves the
    protection alone on this one, at its own price, so the ordering stops
    being a question rather than being guessed. Where no deferral is
    configured the caller sees the raise, which is the fail-closed default.
    """
    if default_fill == "open":
        return

    if isinstance(pending_decision, PortfolioWeights):
        # A whole-book target prices every open position, so each one overlaps
        # the batch whether or not the symbol appears in the target weights.
        decision_symbols = set(positions)
    else:
        decision_symbols = {
            intent.symbol or primary_symbol
            for intent in pending_decision
            if not _intent_executes_at_open(
                intent,
                bars.get(intent.symbol or primary_symbol, {}),
                default_fill,
            )
        }
    # WHY: a carried residual fills at this bar's price like any other
    # decision, and it reaches here with no pending decision of its own. Those
    # are the bars most likely to collide, because a residual persists across
    # bars while a protection can trigger on any of them.
    if rebalance_state is not None:
        decision_symbols |= {order.intent.symbol for order in rebalance_state.orders}
        decision_symbols |= {leg.symbol for leg in rebalance_state.unresolved_legs}
    if not decision_symbols:
        return

    conflicts = sorted(
        symbol
        for symbol in set(positions) & decision_symbols & set(bars)
        # WHY: a market exit carried from an earlier bar resumes at this bar's
        # open, ahead of any close/high/low fill, so its ordering is defined.
        # resolve_stop_exit reports it before reading any trigger level, so
        # only a level triggered by *this* bar is genuinely ambiguous.
        if positions[symbol].pending_market_exit_reason is None
        and resolve_stop_exit(positions[symbol], bars[symbol], get_cost_model(symbol)) is not None
    )
    if conflicts:
        raise AmbiguousBarOrderingError(conflicts)


def execute_pending_decision_and_stops(
    ts: datetime,
    positions: dict[str, PositionState],
    cash: float,
    pending_decision: StrategyDecision,
    bars: dict[str, dict[str, float]],
    *,
    get_cost_model: Callable[[str], CostModel],
    default_fill: str,
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_order_notional: float | None = None,
    max_bar_volume_participation_rate: float | None = None,
    max_adv_participation_rate: float | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
    max_gross_exposure: float | None = None,
    max_net_exposure: float | None = None,
    exposure_prices: Mapping[str, float] | None = None,
    rebalance_state: PortfolioRebalanceState | None = None,
    rebalance_residual_policy: RebalanceResidualPolicy = "discard",
) -> tuple[float, ExecutionResult]:
    """Fill pending decisions, then check causally eligible protective exits.

    Positions already open before this bar and entries known to fill at the
    bar open may trigger protection on this bar. Protection on a new resting
    limit or non-open field fill starts on the next bar because OHLCV cannot
    establish whether the bar's stop/target occurred before or after entry.
    Backtest and real-time simulation share this conservative convention.
    Live broker execution intentionally does not call it.

    Returns (updated cash, combined ExecutionResult for both steps).
    """
    trades: list[TradeResult] = []
    events: list[PositionEvent] = []
    runtime_events: list[RuntimeEvent] = []
    cash_delta_total = 0.0
    next_rebalance_state: PortfolioRebalanceState | None = None
    used_bar_quantity_by_symbol: dict[str, float] = {}
    same_bar_protection_symbols = set(positions)

    if pending_decision and rebalance_state is not None:
        raise ValueError("cannot execute a new decision while a portfolio residual is pending")

    if pending_decision or rebalance_state is not None:
        _validate_no_ambiguous_stop_conflicts(
            pending_decision,
            positions,
            bars,
            get_cost_model=get_cost_model,
            default_fill=default_fill,
            primary_symbol=primary_symbol,
            rebalance_state=rebalance_state,
        )
        enforce_portfolio_limits = max_gross_exposure is not None or max_net_exposure is not None
        strict_rebalance = (
            isinstance(pending_decision, PortfolioWeights) and rebalance_residual_policy == "fail"
        )
        retained_rebalance = (
            isinstance(pending_decision, PortfolioWeights) or rebalance_state is not None
        ) and rebalance_residual_policy != "discard"
        stage_execution = enforce_portfolio_limits or retained_rebalance
        if enforce_portfolio_limits and exposure_prices is None:
            raise ValueError("portfolio exposure limits require explicit exposure_prices")
        execution_positions = deepcopy(positions) if stage_execution else positions
        execution_adv_quantities = (
            dict(used_adv_quantity_by_symbol or {}) if stage_execution else None
        )
        adv_quantities = (
            execution_adv_quantities if stage_execution else used_adv_quantity_by_symbol
        )

        if isinstance(pending_decision, PortfolioWeights):
            if default_fill == "open":
                same_bar_protection_symbols.update(pending_decision.weights)
        elif rebalance_state is not None:
            if default_fill == "open":
                same_bar_protection_symbols.update(
                    order.intent.symbol
                    for order in rebalance_state.orders
                    if order.phase == "addition"
                )
        else:
            for intent in pending_decision:
                symbol = intent.symbol or primary_symbol
                if intent.action in ("long", "short") and _intent_executes_at_open(
                    intent,
                    bars.get(symbol, {}),
                    default_fill,
                ):
                    same_bar_protection_symbols.add(symbol)

        def get_price(sym: str, action: OrderIntent) -> float | None:
            return resolve_fill_price(
                bars.get(sym, {}),
                action,
                default_fill=default_fill,
                position_side=(
                    execution_positions[sym].side if sym in execution_positions else None
                ),
            )

        def get_fresh_reference_price(sym: str) -> float | None:
            raw_price = bars.get(sym, {}).get(default_fill)
            if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
                return None
            return float(raw_price)

        def get_valuation_price(sym: str) -> float | None:
            return get_fresh_reference_price(sym) or (exposure_prices or {}).get(sym)

        common_kwargs = {
            "get_price": get_price,
            "get_cost_model": get_cost_model,
            "primary_symbol": primary_symbol,
            "max_position_notional": max_position_notional,
            "max_order_notional": max_order_notional,
            "max_bar_volume_participation_rate": max_bar_volume_participation_rate,
            "max_adv_participation_rate": max_adv_participation_rate,
            "get_volume": lambda sym: bars.get(sym, {}).get("volume"),
            "get_lagged_adv": get_lagged_adv,
        }
        if isinstance(pending_decision, PortfolioWeights) or rebalance_state is not None:
            if rebalance_residual_policy == "discard":
                if rebalance_state is not None:
                    raise ValueError("discard policy cannot retain a portfolio rebalance residual")
                assert isinstance(pending_decision, PortfolioWeights)
                fill_result = execute_portfolio_weights(
                    pending_decision,
                    execution_positions,
                    cash,
                    ts,
                    get_reference_price=lambda sym: bars.get(sym, {}).get(default_fill),
                    used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=adv_quantities,
                    **common_kwargs,
                )
            else:
                state = rebalance_state
                if state is None:
                    assert isinstance(pending_decision, PortfolioWeights)
                    state, _ = _plan_portfolio_weights(
                        pending_decision,
                        execution_positions,
                        cash,
                        get_reference_price=get_valuation_price,
                        get_fresh_price=get_fresh_reference_price,
                        get_cost_model=get_cost_model,
                    )
                fill_result = execute_portfolio_rebalance_slice(
                    state,
                    execution_positions,
                    cash,
                    ts,
                    residual_policy=rebalance_residual_policy,
                    get_fresh_price=get_fresh_reference_price,
                    used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
                    used_adv_quantity_by_symbol=adv_quantities,
                    **common_kwargs,
                )
                if strict_rebalance and fill_result.pending_rebalance is not None:
                    residual_symbols = sorted(
                        {order.intent.symbol for order in fill_result.pending_rebalance.orders}
                        | {leg.symbol for leg in fill_result.pending_rebalance.unresolved_legs}
                    )
                    raise ValueError(
                        "PortfolioWeights fail policy rejected incomplete execution; "
                        f"residual remains for {residual_symbols}"
                    )
        else:
            # execute_order_intents raises rather than silently skipping a
            # leg with no price, so a grouped decision never fills some legs
            # and not others.
            fill_result = execute_order_intents(
                pending_decision,
                execution_positions,
                cash,
                ts,
                **common_kwargs,
                used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=adv_quantities,
                atomic_groups=True,
            )
        if enforce_portfolio_limits:
            assert exposure_prices is not None
            validation_prices = dict(exposure_prices)
            validation_prices.update({event.symbol: event.price for event in fill_result.events})
            validate_exposure_transition(
                positions_before=positions,
                cash_before=cash,
                positions_after=execution_positions,
                cash_after=cash + fill_result.cash_delta,
                prices=validation_prices,
                get_cost_model=get_cost_model,
                max_gross_exposure=max_gross_exposure,
                max_net_exposure=max_net_exposure,
            )
        if stage_execution:
            positions.clear()
            positions.update(execution_positions)
            if used_adv_quantity_by_symbol is not None and execution_adv_quantities is not None:
                used_adv_quantity_by_symbol.clear()
                used_adv_quantity_by_symbol.update(execution_adv_quantities)
        trades.extend(fill_result.trades)
        events.extend(fill_result.events)
        runtime_events.extend(fill_result.runtime_events)
        next_rebalance_state = fill_result.pending_rebalance
        cash_delta_total += fill_result.cash_delta
        cash += fill_result.cash_delta

    if positions:
        stop_result = check_stop_targets(
            positions,
            bars,
            ts,
            get_cost_model=get_cost_model,
            max_bar_volume_participation_rate=max_bar_volume_participation_rate,
            max_adv_participation_rate=max_adv_participation_rate,
            get_lagged_adv=get_lagged_adv,
            used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
            used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            eligible_symbols=same_bar_protection_symbols,
        )
        trades.extend(stop_result.trades)
        events.extend(stop_result.events)
        runtime_events.extend(stop_result.runtime_events)
        protective_exit_reasons = {
            event.symbol: event.reason
            for event in stop_result.events
            if event.reason in (REASON_LIQUIDATION, REASON_STOP_LOSS, REASON_TAKE_PROFIT)
        }
        protective_exit_reasons.update(
            {
                event.symbol: str(event.detail["exit_reason"])
                for event in stop_result.runtime_events
                if event.symbol is not None
                and event.detail.get("reason") == "protective_exit_deferred"
            }
        )
        if next_rebalance_state is not None and protective_exit_reasons:
            next_rebalance_state, cancellation_events = _cancel_rebalance_symbols(
                next_rebalance_state,
                protective_exit_reasons,
                ts,
            )
            runtime_events.extend(cancellation_events)
        cash_delta_total += stop_result.cash_delta
        cash += stop_result.cash_delta

    return cash, ExecutionResult(
        trades=trades,
        events=events,
        cash_delta=cash_delta_total,
        runtime_events=coalesce_runtime_events(runtime_events),
        pending_rebalance=next_rebalance_state,
    )
