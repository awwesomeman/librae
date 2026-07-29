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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal

from librae.core import EPSILON

from .cost_model import CostModel
from .strategy import (
    Fill,
    MultiLegOrder,
    OrderAction,
    OrderIntent,
    PortfolioTargets,
    Position,
    PositionEventType,
    PositionSide,
    PositionState,
    StrategyDecision,
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
class OrderEvent:
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
    pnl: float | None = None
    net_return: float | None = None
    entry_at: datetime | None = None
    periods_held: int | None = None
    reason: str = ""
    entry_commission: float | None = None
    entry_slippage: float | None = None
    entry_tax: float | None = None


@dataclass
class ExecutionResult:
    """Results from executing one decision on one bar."""

    trades: list[TradeResult]
    events: list[OrderEvent]
    cash_delta: float


def side_multiplier(side: PositionSide) -> float:
    """Convert side to direction multiplier. +1 for long, -1 for short."""
    return -1.0 if side == "short" else 1.0


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
) -> tuple[TradeResult, OrderEvent, float, bool]:
    """Close a position (full/partial) and build its TradeResult + OrderEvent together.

    Single place that turns a "close at this price" decision into the trade
    record + lifecycle event, whoever the caller is (strategy-driven close,
    stop-loss/take-profit trigger, end-of-run force-close). Keeps the three
    call sites from hand-rolling slightly different OrderEvent constructions.

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
    event = OrderEvent(
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
        pnl=pnl.net_pnl,
        net_return=pnl.net_return,
        entry_at=pos.entry_at,
        periods_held=pos.periods_held,
        reason=reason,
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
            )
            positions[symbol] = position
        else:
            scale_into_position(position, fill, cost_model)

        event = OrderEvent(
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
    event = OrderEvent(
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
        pnl=net_pnl,
        net_return=net_return,
        entry_at=position.entry_at,
        periods_held=position.periods_held,
        reason=reason,
    )

    margin_locked = entry_notional * cost_model.margin_rate(position.side)
    proceeds = margin_locked + gross_pnl - costs
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
    events: list[OrderEvent] = []
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

    return ExecutionResult(trades=trades, events=events, cash_delta=cash_delta)


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

    Shared by end-of-run liquidation and the max-drawdown circuit breaker —
    both need identical liquidity and impact semantics. Mutates *positions*
    in place. A constrained exit can be partial and is retried on a later bar.
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta = 0.0

    for sym in list(positions.keys()):
        pos = positions[sym]
        bar = bars.get(sym)
        if bar is None or bar.get("close") is None:
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

    return ExecutionResult(trades=trades, events=events, cash_delta=cash_delta)


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
    """Resolve actual fill price from ``intent.fill_price`` or engine default.

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
    fill_spec = intent.fill_price if intent.fill_price is not None else default_fill

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

        if intent.action == "long":
            order_side: Literal["buy", "sell"] | None = "buy"
        elif intent.action == "short":
            order_side = "sell"
        elif intent.action == "close" and position_side is not None:
            order_side = "sell" if position_side == "long" else "buy"
        else:
            order_side = None
        if order_side is None:
            logger.warning("Limit order rejected: cannot infer order side for %s", intent.action)
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
) -> tuple[Fill | None, float]:
    """Attempt a fill and validate cash sufficiency. Returns (fill, outlay) or (None, 0)."""
    fill = simulate_fill(action, price, available_cash, cost_model, bar_volume=bar_volume)
    if not fill or fill.quantity <= 0:
        return None, 0.0
    if max_notional is not None:
        fill = _cap_fill_to_notional(
            fill, existing_qty, cost_model, max_notional, bar_volume=bar_volume
        )
        if fill is None:
            return None, 0.0
    if max_volume_qty is not None:
        fill = _cap_fill_to_volume(fill, cost_model, max_volume_qty, bar_volume=bar_volume)
        if fill is None:
            return None, 0.0
    outlay = cost_model.estimate_entry_outlay(
        price,
        fill.quantity,
        action.action,
        bar_volume=bar_volume,
    )
    if available_cash - outlay < -EPSILON:
        return None, 0.0
    return fill, outlay


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
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta = 0.0
    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}

    for action in intents:
        sym = action.symbol or primary_symbol
        price_raw = get_price(sym, action)
        if price_raw is None or price_raw <= 0:
            continue
        price = float(price_raw)
        cost_model = get_cost_model(sym)
        reason = action.reason

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
                fill, outlay = _try_fill(
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
                    )
                    events.append(
                        OrderEvent(
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
                        )
                    )
                    volume_consumed[sym] = volume_consumed.get(sym, 0.0) + fill.quantity
                    adv_consumed[sym] = adv_consumed.get(sym, 0.0) + fill.quantity

            elif positions[sym].side == desired_side:
                # SCALE IN — must specify quantity. Same severity as the
                # opposite-side rejection below: both are a strategy action
                # silently turned into a no-op, not a normal/expected path.
                if action.quantity is None:
                    logger.warning("Scaling %s requires explicit quantity, skipping", sym)
                    continue
                fill, outlay = _try_fill(
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
                    events.append(
                        OrderEvent(
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
                        )
                    )
                    volume_consumed[sym] = volume_consumed.get(sym, 0.0) + fill.quantity
                    adv_consumed[sym] = adv_consumed.get(sym, 0.0) + fill.quantity

            else:
                # OPPOSITE SIDE — reject
                logger.warning(
                    "Rejected %s %s: already %s — close first",
                    action.action,
                    sym,
                    positions[sym].side,
                )

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
                continue

            trade, event, proceeds, fully_closed = build_close_event(
                pos,
                ts,
                price,
                cost_model,
                reason,
                quantity=requested_qty,
                bar_volume=bar_volume,
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

    return ExecutionResult(trades=trades, events=events, cash_delta=cash_delta)


def _scale_additions_to_cash(
    actions: list[OrderIntent],
    available_cash: float,
    *,
    prices: dict[str, float],
    get_cost_model: Callable[[str], CostModel],
    get_volume: Callable[[str], float | None] | None = None,
) -> list[OrderIntent]:
    """Scale all rebalance additions by one factor when cash is insufficient.

    A common factor preserves the intended cross-sectional allocation better
    than accepting actions in symbol order until cash runs out. The binary
    search accounts for nonlinear minimum commissions.
    """
    if not actions or available_cash <= EPSILON:
        return []

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
        return actions

    low, high = 0.0, 1.0
    for _ in range(60):
        middle = (low + high) / 2.0
        if total_outlay(middle) <= available_cash:
            low = middle
        else:
            high = middle

    if low <= EPSILON:
        logger.warning("Rebalance additions skipped: insufficient cash after reductions")
        return []

    logger.info("Rebalance additions scaled to %.6f of requested quantities", low)
    return [
        OrderIntent(
            action=action.action,
            symbol=action.symbol,
            quantity=(action.quantity or 0.0) * low,
            reason=action.reason,
            fill_price=action.fill_price,
        )
        for action in actions
    ]


def execute_portfolio_targets(
    targets: PortfolioTargets,
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
    max_gross_exposure: float | None = None,
    max_net_exposure: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
    get_lagged_adv: Callable[[str], float | None] | None = None,
    used_bar_quantity_by_symbol: dict[str, float] | None = None,
    used_adv_quantity_by_symbol: dict[str, float] | None = None,
) -> ExecutionResult:
    """Resolve and execute a portfolio rebalance as one deterministic batch.

    All relevant execution prices are resolved before mutating the portfolio.
    Existing exposure is reduced first, then additions are submitted in symbol
    order. If transaction costs make the additions unaffordable, every addition
    is scaled by the same factor instead of starving later symbols.
    """
    volume_consumed = used_bar_quantity_by_symbol if used_bar_quantity_by_symbol is not None else {}
    adv_consumed = used_adv_quantity_by_symbol if used_adv_quantity_by_symbol is not None else {}
    target_gross_exposure = sum(abs(weight) for weight in targets.weights.values())
    target_net_exposure = abs(sum(targets.weights.values()))
    if max_gross_exposure is not None and target_gross_exposure > max_gross_exposure + EPSILON:
        raise ValueError(
            f"target gross exposure {target_gross_exposure:.6f} exceeds "
            f"max_gross_exposure={max_gross_exposure:.6f}"
        )
    if max_net_exposure is not None and target_net_exposure > max_net_exposure + EPSILON:
        raise ValueError(
            f"absolute target net exposure {target_net_exposure:.6f} exceeds "
            f"max_net_exposure={max_net_exposure:.6f}"
        )

    target_symbols = {symbol for symbol, weight in targets.weights.items() if abs(weight) > EPSILON}
    relevant_symbols = sorted(set(positions) | target_symbols)
    if not relevant_symbols:
        return ExecutionResult(trades=[], events=[], cash_delta=0.0)

    prices: dict[str, float] = {}
    for symbol in relevant_symbols:
        target_weight = targets.weights.get(symbol, 0.0)
        action_type: OrderAction
        if target_weight > EPSILON:
            action_type = "long"
        elif target_weight < -EPSILON:
            action_type = "short"
        else:
            action_type = "close"
        price_action = OrderIntent(
            action=action_type,
            symbol=symbol,
            reason=targets.reason,
            fill_price=targets.fill_price,
        )
        raw_price = get_price(symbol, price_action)
        if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
            raise ValueError(f"rebalance requires a valid execution price for {symbol}")
        prices[symbol] = float(raw_price)

    equity, _ = calc_equity(
        cash,
        positions,
        get_price=lambda symbol, _position: prices[symbol],
        get_cost_model=get_cost_model,
    )
    if equity <= EPSILON:
        raise ValueError("rebalance requires positive execution-time equity")

    reductions: list[OrderIntent] = []
    additions: list[OrderIntent] = []
    for symbol in relevant_symbols:
        position = positions.get(symbol)
        current_signed_quantity = 0.0
        if position is not None:
            current_signed_quantity = position.quantity * side_multiplier(position.side)

        cost_model = get_cost_model(symbol)
        target_weight = targets.weights.get(symbol, 0.0)
        target_signed_quantity = target_weight * equity / (prices[symbol] * cost_model.multiplier)

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
                        reason=targets.reason,
                        fill_price=targets.fill_price,
                    )
                )
            elif quantity_delta > EPSILON:
                additions.append(
                    OrderIntent(
                        action="long" if target_signed_quantity > 0 else "short",
                        symbol=symbol,
                        quantity=quantity_delta,
                        reason=targets.reason,
                        fill_price=targets.fill_price,
                    )
                )
            continue

        reductions.append(
            OrderIntent(
                action="close",
                symbol=symbol,
                reason=targets.reason,
                fill_price=targets.fill_price,
            )
        )
        additions.append(
            OrderIntent(
                action="long" if target_signed_quantity > 0 else "short",
                symbol=symbol,
                quantity=abs(target_signed_quantity),
                reason=targets.reason,
                fill_price=targets.fill_price,
            )
        )

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
    scaled_additions = _scale_additions_to_cash(
        additions,
        cash_after_reductions,
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
    )


# ---------------------------------------------------------------------------
# Combined pending-fill + stop-check step (backtest/sim)
# ---------------------------------------------------------------------------


def validate_strategy_decision(
    decision: StrategyDecision,
    universe: set[str],
    *,
    primary_symbol: str,
) -> None:
    """Validate one strategy return value before it enters engine state."""
    if isinstance(decision, PortfolioTargets):
        symbols = set(decision.weights)
    elif isinstance(decision, MultiLegOrder):
        symbols = {leg.symbol for leg in decision.legs}
    else:
        if not isinstance(decision, list):
            raise TypeError(
                "strategy decision must be list[OrderIntent], PortfolioTargets, or MultiLegOrder"
            )
        invalid = [
            type(intent).__name__ for intent in decision if not isinstance(intent, OrderIntent)
        ]
        if invalid:
            raise TypeError(f"strategy decision contains non-OrderIntent values: {invalid}")
        resolved_symbols = [intent.symbol or primary_symbol for intent in decision]
        if len(resolved_symbols) != len(set(resolved_symbols)):
            raise ValueError("strategy decision must contain at most one intent per symbol")
        symbols = set(resolved_symbols)
    unknown = symbols - universe
    if unknown:
        raise ValueError(f"strategy decision contains unknown symbols: {sorted(unknown)}")


def _intent_executes_at_open(
    intent: OrderIntent,
    bar: dict[str, float],
    default_fill: str,
) -> bool:
    """Return whether an entry is causally known to exist from bar open."""
    fill_spec = intent.fill_price if intent.fill_price is not None else default_fill
    if isinstance(fill_spec, str):
        return fill_spec == "open"

    open_raw = bar.get("open")
    if open_raw is None:
        return False
    open_price = float(open_raw)
    limit_price = float(fill_spec)
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

    Per-symbol order intents become eligible on that symbol's next observed bar.
    PortfolioTargets and MultiLegOrder remain synchronous and wait until every
    required symbol has a bar.
    """
    if isinstance(decision, PortfolioTargets):
        target_symbols = {
            symbol for symbol, weight in decision.weights.items() if abs(weight) > EPSILON
        }
        required_symbols = set(positions) | target_symbols
        if required_symbols.issubset(bars):
            return decision, []
        return [], decision
    if isinstance(decision, MultiLegOrder):
        if {leg.symbol for leg in decision.legs}.issubset(bars):
            return decision, []
        return [], decision

    ready: list[OrderIntent] = []
    waiting: list[OrderIntent] = []
    for intent in decision:
        symbol = intent.symbol or primary_symbol
        if symbol in bars:
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
    """Merge independent per-symbol intents without replacing pending ones."""
    if not pending:
        return new_decision
    if not new_decision:
        return pending
    if isinstance(pending, (PortfolioTargets, MultiLegOrder)) or isinstance(
        new_decision,
        (PortfolioTargets, MultiLegOrder),
    ):
        raise ValueError("cannot replace a pending grouped decision")

    pending_intents = list(pending)
    new_intents = list(new_decision)

    pending_symbols = {intent.symbol or primary_symbol for intent in pending_intents}
    new_symbols = {intent.symbol or primary_symbol for intent in new_intents}
    overlap = pending_symbols & new_symbols
    if overlap:
        raise ValueError(f"strategy emitted duplicate pending intents for {sorted(overlap)}")
    return [*pending_intents, *new_intents]


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
    events: list[OrderEvent] = []
    cash_delta_total = 0.0
    used_bar_quantity_by_symbol: dict[str, float] = {}
    same_bar_protection_symbols = set(positions)

    if pending_decision:
        if isinstance(pending_decision, PortfolioTargets):
            effective_fill = pending_decision.fill_price or default_fill
            if effective_fill == "open":
                same_bar_protection_symbols.update(pending_decision.weights)
        else:
            intents = (
                pending_decision.legs
                if isinstance(pending_decision, MultiLegOrder)
                else pending_decision
            )
            for intent in intents:
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
                position_side=positions[sym].side if sym in positions else None,
            )

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
        if isinstance(pending_decision, PortfolioTargets):
            fill_result = execute_portfolio_targets(
                pending_decision,
                positions,
                cash,
                ts,
                max_gross_exposure=max_gross_exposure,
                max_net_exposure=max_net_exposure,
                used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
                **common_kwargs,
            )
        else:
            intents = (
                list(pending_decision.legs)
                if isinstance(pending_decision, MultiLegOrder)
                else pending_decision
            )
            fill_result = execute_order_intents(
                intents,
                positions,
                cash,
                ts,
                **common_kwargs,
                used_bar_quantity_by_symbol=used_bar_quantity_by_symbol,
                used_adv_quantity_by_symbol=used_adv_quantity_by_symbol,
            )
        trades.extend(fill_result.trades)
        events.extend(fill_result.events)
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
        cash_delta_total += stop_result.cash_delta
        cash += stop_result.cash_delta

    return cash, ExecutionResult(trades=trades, events=events, cash_delta=cash_delta_total)
