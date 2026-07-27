"""Execution layer — separates trade execution from engine logic.

Contains:
- make_fill(): pure function for simulated fills (backtest uses directly)
- _size_position(): position sizing using all available cash
- calc_trade_pnl(): shared PnL calculation for backtest + live
- scale_into_position(): add to existing position (weighted avg)
- reduce_position(): shrink position after partial close
- close_position(): full or partial close with correct proceeds
- process_actions(): deterministic simulated action loop and live request planner
- TradePnL: PnL breakdown dataclass

Position sizing is the strategy's responsibility (set Action.quantity).
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
from .strategy import Action, Fill, Position, PositionState, RebalanceTargets, StrategyIntent

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
    side: Literal["long", "short"]
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
    """Single position lifecycle event — open/add/reduce/close."""

    ts: datetime
    symbol: str
    side: Literal["long", "short"]
    event_type: Literal["open", "add", "reduce", "close"]
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


@dataclass
class ActionResults:
    """Results from processing one bar's actions."""

    trades: list[TradeResult]
    events: list[OrderEvent]
    cash_delta: float


def direction(side: Literal["long", "short"]) -> float:
    """Convert side to direction multiplier. +1 for long, -1 for short."""
    return -1.0 if side == "short" else 1.0


# ---------------------------------------------------------------------------
# Position snapshot + MTM
# ---------------------------------------------------------------------------


def eval_equity(
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
        unrealized = cost_model.calc_pnl(ps.entry_price, price, ps.quantity) * direction(ps.side)
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
    side: Literal["long", "short"],
    cost_model: CostModel,
    entry_commission: float,
    entry_slippage: float,
    entry_tax: float = 0.0,
) -> TradePnL:
    """Single trade PnL breakdown. Used by backtest + live."""
    dir_mult = direction(side)
    gross_pnl = cost_model.calc_pnl(entry_price, exit_price, quantity) * dir_mult

    exit_commission = cost_model.calc_commission(exit_price, quantity)
    exit_slippage = cost_model.calc_slippage(quantity)
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
) -> tuple[TradeResult, OrderEvent, float, bool]:
    """Close a position (full/partial) and build its TradeResult + OrderEvent together.

    Single place that turns a "close at this price" decision into the trade
    record + lifecycle event, whoever the caller is (strategy-driven close,
    stop-loss/take-profit trigger, end-of-run force-close). Keeps the three
    call sites from hand-rolling slightly different OrderEvent constructions.

    Returns (trade, event, cash_proceeds, fully_closed).
    """
    close_qty = min(quantity, pos.quantity) if quantity is not None else pos.quantity
    pnl, proceeds, fully_closed = close_position(pos, exit_price, cost_model, quantity=close_qty)
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
        commission=pnl.commission,
        slippage=pnl.slippage,
        tax=pnl.tax,
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
) -> tuple[float, ActionResults]:
    """Apply one externally confirmed execution to portfolio state.

    Unlike :func:`process_actions`, this function does not simulate price,
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
        result = ActionResults(trades=[], events=[event], cash_delta=-outlay)
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
    gross_pnl = cost_model.calc_pnl(position.entry_price, fill.price, close_quantity) * direction(
        position.side
    )
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
        commission=total_commission,
        slippage=total_slippage,
        tax=total_tax,
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

    result = ActionResults(trades=[trade], events=[event], cash_delta=proceeds)
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
    fill); take_profit_price is modeled as a limit order (fills exactly at
    that price once touched). Stop-loss is checked before take-profit — if
    both would trigger on the same bar, the conservative outcome wins.

    Returns (fill_price, reason) or None if nothing is triggered.
    """
    high, low, open_ = bar.get("high"), bar.get("low"), bar.get("open")
    if high is None or low is None or open_ is None:
        return None
    is_long = pos.side == "long"

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
) -> ActionResults:
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
        bar = bars.get(sym)
        if bar is None:
            continue
        pos = positions[sym]
        cost_model = get_cost_model(sym)
        hit = resolve_stop_exit(pos, bar, cost_model)
        if hit is None:
            continue
        price, reason = hit
        trade, event, proceeds, _ = build_close_event(pos, ts, price, cost_model, reason)
        trades.append(trade)
        events.append(event)
        cash_delta += proceeds
        del positions[sym]

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)


def validate_risk_params(
    params: dict | None,
) -> tuple[float | None, float | None, float | None]:
    """Validate and extract max_position_pct/max_drawdown_pct/
    max_volume_participation_pct from cfg.params.

    All are optional (None = disabled) but must be > 0 if set. Shared by
    Backtest and LiveTrader so the same rule can't drift between them.
    """
    p = params or {}
    max_position_pct = p.get("max_position_pct")
    max_drawdown_pct = p.get("max_drawdown_pct")
    max_volume_participation_pct = p.get("max_volume_participation_pct")
    if max_position_pct is not None and max_position_pct <= 0:
        raise ValueError(f"max_position_pct must be > 0, got {max_position_pct}")
    if max_drawdown_pct is not None and max_drawdown_pct <= 0:
        raise ValueError(f"max_drawdown_pct must be > 0, got {max_drawdown_pct}")
    if max_volume_participation_pct is not None and not (0 < max_volume_participation_pct <= 1):
        raise ValueError(
            f"max_volume_participation_pct must be in (0, 1], got {max_volume_participation_pct}"
        )
    return max_position_pct, max_drawdown_pct, max_volume_participation_pct


def liquidate_all(
    positions: dict[str, PositionState],
    bars: dict[str, dict[str, float]],
    ts: datetime,
    *,
    get_cost_model: Callable[[str], CostModel],
    reason: str,
    fallback_price: Callable[[str, PositionState], float] | None = None,
) -> ActionResults:
    """Force-close every open position right now, at this bar's close price.

    Shared by end-of-run liquidation and the max-drawdown circuit breaker —
    both need identical "flatten everything" semantics. Mutates *positions*
    in place. fallback_price is used only when a symbol has no bar this
    step (e.g. the backtest's final bar, which falls back to entry_price).
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta = 0.0

    for sym in list(positions.keys()):
        pos = positions[sym]
        bar = bars.get(sym)
        if bar is not None and bar.get("close") is not None:
            price = bar["close"]
        elif fallback_price is not None:
            price = fallback_price(sym, pos)
        else:
            continue
        cost_model = get_cost_model(sym)
        trade, event, proceeds, _ = build_close_event(pos, ts, price, cost_model, reason)
        trades.append(trade)
        events.append(event)
        cash_delta += proceeds
        del positions[sym]

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)


# ---------------------------------------------------------------------------
# Simulated fill price resolution (backtest/sim)
# ---------------------------------------------------------------------------


def resolve_fill_price(
    bar: dict[str, float],
    action: Action,
    default_fill: str,
    *,
    position_side: Literal["long", "short"] | None = None,
) -> float | None:
    """Resolve actual fill price from action.fill_price or engine default.

    Args:
        bar: Next bar's OHLCV dict (the bar where the fill happens).
        action: The action whose fill_price spec to use.
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
    fill_spec = action.fill_price if action.fill_price is not None else default_fill

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

        if action.type == "long":
            order_side: Literal["buy", "sell"] | None = "buy"
        elif action.type == "short":
            order_side = "sell"
        elif action.type == "close" and position_side is not None:
            order_side = "sell" if position_side == "long" else "buy"
        else:
            order_side = None
        if order_side is None:
            logger.warning("Limit order rejected: cannot infer order side for %s", action.type)
            return None

        open_raw = bar.get("open")
        open_price = float(open_raw) if open_raw is not None else limit
        if not isfinite(open_price) or open_price <= 0:
            open_price = limit

        reached = low <= limit if order_side == "buy" else high >= limit
        if not reached:
            logger.info(
                "Limit intent expired unfilled: %s %s at %.6f",
                order_side,
                action.symbol,
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
    cost_model: CostModel, price: float, cash: float, side: Literal["long", "short"]
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
    return max(qty, 0.0)


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
    """Shrink a fill to at most max_qty (typically max_volume_participation_pct
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


def make_fill(
    action: Action,
    price: float,
    cash: float,
    cost_model: CostModel,
    *,
    bar_volume: float | None = None,
) -> Fill | None:
    """Build a Fill for a long/short action. Returns None if rejected."""
    if action.type not in ("long", "short"):
        return None

    qty = action.quantity
    if qty is None:
        qty = _size_position(cost_model, price, cash, action.type)
    if qty <= 0:
        return None

    return Fill(
        symbol=action.symbol,
        side=action.type,
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
    action: Action,
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
    fill = make_fill(action, price, available_cash, cost_model, bar_volume=bar_volume)
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
    outlay = cost_model.estimate_entry_outlay(price, fill.quantity, action.type)
    if available_cash - outlay < -EPSILON:
        return None, 0.0
    return fill, outlay


def process_actions(
    actions: list[Action],
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    get_price: Callable[[str, Action], float | None],
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_volume_participation_pct: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
) -> ActionResults:
    """Process a bar's actions: open, scale, partial/full close.

    Mutates *positions* dict in place. Backtest/sim uses the resulting fills;
    live may use it only on a copied portfolio to size order requests.

    max_position_notional, when set, caps every symbol's post-fill notional
    (existing + added) to this value — applied identically to new entries
    and scale-ins.

    max_volume_participation_pct, when set together with get_volume, caps
    each fill (not cumulative like max_position_notional) to that fraction
    of the symbol's bar volume. get_volume also feeds CostModel.calc_slippage's
    participation-scaled impact component even when no cap is configured.
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta = 0.0

    for action in actions:
        if action.type == "hold":
            continue

        sym = action.symbol or primary_symbol
        price_raw = get_price(sym, action)
        if price_raw is None or price_raw <= 0:
            continue
        price = float(price_raw)
        cost_model = get_cost_model(sym)
        reason = action.reason

        if action.type in ("long", "short"):
            desired_side: Literal["long", "short"] = action.type

            bar_volume = get_volume(sym) if get_volume else None
            # WHY: `bar_volume is not None`, not truthy — a real zero-volume
            # bar must cap to 0 (reject the fill outright), not be treated
            # the same as "no volume data available" (which skips the cap).
            max_volume_qty = (
                max_volume_participation_pct * bar_volume
                if max_volume_participation_pct and bar_volume is not None
                else None
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

            else:
                # OPPOSITE SIDE — reject
                logger.warning(
                    "Rejected %s %s: already %s — close first",
                    action.type,
                    sym,
                    positions[sym].side,
                )

        elif action.type == "close" and sym in positions:
            pos = positions[sym]
            close_qty = action.quantity

            # Reject zero-quantity close
            if close_qty is not None and close_qty <= 0:
                continue

            trade, event, proceeds, fully_closed = build_close_event(
                pos,
                ts,
                price,
                cost_model,
                reason,
                quantity=close_qty,
            )
            trades.append(trade)
            events.append(event)
            cash_delta += proceeds

            if fully_closed:
                del positions[sym]
            else:
                reduce_position(pos, trade.quantity)

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)


def _scale_additions_to_cash(
    actions: list[Action],
    available_cash: float,
    *,
    prices: dict[str, float],
    get_cost_model: Callable[[str], CostModel],
) -> list[Action]:
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
            side: Literal["long", "short"] = "short" if action.type == "short" else "long"
            total += cost_model.estimate_entry_outlay(price, quantity, side)
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
        Action(
            type=action.type,
            symbol=action.symbol,
            quantity=(action.quantity or 0.0) * low,
            reason=action.reason,
            fill_price=action.fill_price,
        )
        for action in actions
    ]


def process_rebalance_targets(
    targets: RebalanceTargets,
    positions: dict[str, PositionState],
    cash: float,
    ts: datetime,
    *,
    get_price: Callable[[str, Action], float | None],
    get_cost_model: Callable[[str], CostModel],
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_volume_participation_pct: float | None = None,
    get_volume: Callable[[str], float | None] | None = None,
) -> ActionResults:
    """Resolve and execute a portfolio rebalance as one deterministic batch.

    All relevant execution prices are resolved before mutating the portfolio.
    Existing exposure is reduced first, then additions are submitted in symbol
    order. If transaction costs make the additions unaffordable, every addition
    is scaled by the same factor instead of starving later symbols.
    """
    target_symbols = {symbol for symbol, weight in targets.weights.items() if abs(weight) > EPSILON}
    relevant_symbols = sorted(set(positions) | target_symbols)
    if not relevant_symbols:
        return ActionResults(trades=[], events=[], cash_delta=0.0)

    prices: dict[str, float] = {}
    for symbol in relevant_symbols:
        target_weight = targets.weights.get(symbol, 0.0)
        action_type: Literal["long", "short", "close"]
        if target_weight > EPSILON:
            action_type = "long"
        elif target_weight < -EPSILON:
            action_type = "short"
        else:
            action_type = "close"
        price_action = Action(
            type=action_type,
            symbol=symbol,
            reason=targets.reason,
            fill_price=targets.fill_price,
        )
        raw_price = get_price(symbol, price_action)
        if raw_price is None or not isfinite(raw_price) or raw_price <= 0:
            logger.warning(
                "Rebalance rejected: no valid execution price for %s; portfolio unchanged",
                symbol,
            )
            return ActionResults(trades=[], events=[], cash_delta=0.0)
        prices[symbol] = float(raw_price)

    equity, _ = eval_equity(
        cash,
        positions,
        get_price=lambda symbol, _position: prices[symbol],
        get_cost_model=get_cost_model,
    )
    if equity <= EPSILON:
        logger.warning("Rebalance rejected: execution-time equity is not positive")
        return ActionResults(trades=[], events=[], cash_delta=0.0)

    reductions: list[Action] = []
    additions: list[Action] = []
    for symbol in relevant_symbols:
        position = positions.get(symbol)
        current_signed_quantity = 0.0
        if position is not None:
            current_signed_quantity = position.quantity * direction(position.side)

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
                    Action(
                        type="close",
                        symbol=symbol,
                        quantity=-quantity_delta,
                        reason=targets.reason,
                        fill_price=targets.fill_price,
                    )
                )
            elif quantity_delta > EPSILON:
                additions.append(
                    Action(
                        type="long" if target_signed_quantity > 0 else "short",
                        symbol=symbol,
                        quantity=quantity_delta,
                        reason=targets.reason,
                        fill_price=targets.fill_price,
                    )
                )
            continue

        reductions.append(
            Action(
                type="close",
                symbol=symbol,
                reason=targets.reason,
                fill_price=targets.fill_price,
            )
        )
        additions.append(
            Action(
                type="long" if target_signed_quantity > 0 else "short",
                symbol=symbol,
                quantity=abs(target_signed_quantity),
                reason=targets.reason,
                fill_price=targets.fill_price,
            )
        )

    reduction_result = process_actions(
        reductions,
        positions,
        cash,
        ts,
        get_price=get_price,
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
    )
    cash_after_reductions = cash + reduction_result.cash_delta
    scaled_additions = _scale_additions_to_cash(
        additions,
        cash_after_reductions,
        prices=prices,
        get_cost_model=get_cost_model,
    )
    addition_result = process_actions(
        scaled_additions,
        positions,
        cash_after_reductions,
        ts,
        get_price=get_price,
        get_cost_model=get_cost_model,
        primary_symbol=primary_symbol,
        max_position_notional=max_position_notional,
        max_volume_participation_pct=max_volume_participation_pct,
        get_volume=get_volume,
    )
    return ActionResults(
        trades=[*reduction_result.trades, *addition_result.trades],
        events=[*reduction_result.events, *addition_result.events],
        cash_delta=reduction_result.cash_delta + addition_result.cash_delta,
    )


# ---------------------------------------------------------------------------
# Combined pending-fill + stop-check step (backtest/sim)
# ---------------------------------------------------------------------------


def run_pending_and_stops(
    ts: datetime,
    positions: dict[str, PositionState],
    cash: float,
    pending_intent: StrategyIntent,
    bars: dict[str, dict[str, float]],
    *,
    get_cost_model: Callable[[str], CostModel],
    default_fill: str,
    primary_symbol: str,
    max_position_notional: float | None = None,
    max_volume_participation_pct: float | None = None,
) -> tuple[float, ActionResults]:
    """Fill this bar's pending actions, then check stop-loss/take-profit —
    the two steps that must always run together, in this order, before a
    strategy sees the bar. Backtest and real-time simulation call this same
    implementation so their deterministic bar-fill ordering cannot drift.
    Live broker execution intentionally does not call it.

    Returns (updated cash, combined ActionResults for both steps).
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta_total = 0.0

    if pending_intent:
        process_intent = (
            process_rebalance_targets
            if isinstance(pending_intent, RebalanceTargets)
            else process_actions
        )
        fill_result = process_intent(
            pending_intent,
            positions,
            cash,
            ts,
            get_price=lambda sym, action: resolve_fill_price(
                bars.get(sym, {}),
                action,
                default_fill=default_fill,
                position_side=positions[sym].side if sym in positions else None,
            ),
            get_cost_model=get_cost_model,
            primary_symbol=primary_symbol,
            max_position_notional=max_position_notional,
            max_volume_participation_pct=max_volume_participation_pct,
            get_volume=lambda sym: bars.get(sym, {}).get("volume"),
        )
        trades.extend(fill_result.trades)
        events.extend(fill_result.events)
        cash_delta_total += fill_result.cash_delta
        cash += fill_result.cash_delta

    if positions:
        stop_result = check_stop_targets(positions, bars, ts, get_cost_model=get_cost_model)
        trades.extend(stop_result.trades)
        events.extend(stop_result.events)
        cash_delta_total += stop_result.cash_delta
        cash += stop_result.cash_delta

    return cash, ActionResults(trades=trades, events=events, cash_delta=cash_delta_total)
