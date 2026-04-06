"""Execution layer — separates trade execution from engine logic.

Contains:
- make_fill(): pure function for simulated fills (backtest uses directly)
- _size_position(): position sizing using all available cash
- calc_trade_pnl(): shared PnL calculation for backtest + live
- scale_into_position(): add to existing position (weighted avg)
- reduce_position(): shrink position after partial close
- close_position(): full or partial close with correct proceeds
- process_actions(): shared action loop for backtest + live engines
- TradePnL: PnL breakdown dataclass

Position sizing is the strategy's responsibility (set Action.quantity).
If strategy doesn't specify quantity, executor uses all available cash
for initial entries only. Scaling requires explicit quantity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from librae.core import EPSILON
from .cost_model import CostModel
from .strategy import Action, Fill, Position, PositionState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeResult:
    """Single completed trade — shared by backtest + live engines."""

    symbol: str
    entry_ts: datetime
    exit_ts: datetime
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
    holding_periods: int


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
    quantity: float
    price: float
    entry_price: float
    position_quantity: float
    notional: float
    commission: float
    slippage: float
    tax: float
    pnl: float | None = None
    net_return: float | None = None
    entry_ts: datetime | None = None
    holding_periods: int | None = None
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
        mtm += unrealized + ps.entry_price * ps.quantity * cost_model.multiplier
        snapshot[sym] = Position(
            symbol=sym,
            side=ps.side,
            entry_price=ps.entry_price,
            quantity=ps.quantity,
            entry_ts=ps.entry_ts,
            periods_held=ps.periods_held,
            unrealized_pnl=unrealized,
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
) -> TradePnL:
    """Single trade PnL breakdown. Used by backtest + live."""
    dir_mult = direction(side)
    gross_pnl = cost_model.calc_pnl(entry_price, exit_price, quantity) * dir_mult

    exit_commission = cost_model.calc_commission(exit_price, quantity)
    exit_slippage = cost_model.calc_slippage(quantity)
    # WHY: closing a long = sell (taxed), closing a short = buy (not taxed)
    exit_tax = cost_model.calc_tax(exit_price, quantity, is_sell=(side == "long"))

    total_commission = entry_commission + exit_commission
    total_slippage = entry_slippage + exit_slippage
    net_pnl = gross_pnl - total_commission - total_slippage - exit_tax

    entry_notional = entry_price * quantity * cost_model.multiplier
    gross_return = (gross_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0
    net_return = (net_pnl / entry_notional * 100) if entry_notional > EPSILON else 0.0

    return TradePnL(
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        commission=total_commission,
        slippage=total_slippage,
        tax=exit_tax,
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

    pnl = calc_trade_pnl(
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=close_qty,
        side=pos.side,
        cost_model=cost_model,
        entry_commission=pro_entry_commission,
        entry_slippage=pro_entry_slippage,
    )

    # WHY: proceeds = collateral returned + PnL - exit costs.
    # For longs: entry deducted (entry_notional + entry_costs), exit returns (exit_notional - exit_costs).
    # For shorts: entry deducted (entry_notional + entry_costs) as collateral,
    #   exit returns (entry_notional + gross_pnl - exit_costs).
    entry_notional = pos.entry_price * close_qty * cost_model.multiplier
    if pos.side == "long":
        proceeds = exit_price * close_qty * cost_model.multiplier - pnl.exit_commission - pnl.exit_slippage - pnl.exit_tax
    else:
        proceeds = entry_notional + pnl.gross_pnl - pnl.exit_commission - pnl.exit_slippage - pnl.exit_tax

    return pnl, proceeds, fully_closed


# ---------------------------------------------------------------------------
# Fill price resolution (shared by backtest + live engines)
# ---------------------------------------------------------------------------


def resolve_fill_price(
    bar: dict[str, float],
    action: Action,
    default_fill: str,
) -> float | None:
    """Resolve actual fill price from action.fill_price or engine default.

    Args:
        bar: Next bar's OHLCV dict (the bar where the fill happens).
        action: The action whose fill_price spec to use.
        default_fill: Engine-level default field name (e.g. "open").

    Returns:
        Resolved price, or None if the order should be rejected
        (limit not reachable, field missing/zero).
    """
    fill_spec = action.fill_price if action.fill_price is not None else default_fill

    if isinstance(fill_spec, (int, float)):
        limit = float(fill_spec)
        if limit <= 0:
            return None
        low, high = bar.get("low", 0), bar.get("high", 0)
        if low <= limit <= high:
            return limit
        return None

    if isinstance(fill_spec, str):
        val = bar.get(fill_spec)
        if val is not None and float(val) > 0:
            return float(val)
        logger.warning("fill_price='%s' not found or zero in bar, order rejected", fill_spec)
    return None


# ---------------------------------------------------------------------------
# Fill creation + sizing
# ---------------------------------------------------------------------------


def _size_position(cost_model: CostModel, price: float, cash: float) -> float:
    """Compute position size using all available cash."""
    outlay_per_unit = cost_model.estimate_entry_outlay(price, 1.0)
    if outlay_per_unit < EPSILON:
        return 0.0
    return cash / outlay_per_unit


def make_fill(action: Action, price: float, cash: float, cost_model: CostModel) -> Fill | None:
    """Build a Fill for a buy/sell action. Returns None if rejected."""
    if action.type not in ("buy", "sell"):
        return None

    qty = action.quantity
    if qty is None:
        qty = _size_position(cost_model, price, cash)
    if qty <= 0:
        return None

    return Fill(
        symbol=action.symbol,
        side="long" if action.type == "buy" else "short",
        price=price,
        quantity=qty,
        commission=cost_model.calc_commission(price, qty),
        slippage=cost_model.calc_slippage(qty),
        tax=cost_model.calc_tax(price, qty, is_sell=(action.type == "sell")),
    )


def build_trade_result(
    pos: PositionState,
    exit_ts: datetime,
    exit_price: float,
    close_qty: float,
    pnl: TradePnL,
) -> TradeResult:
    """Build a TradeResult from a (partial or full) close."""
    return TradeResult(
        symbol=pos.symbol,
        entry_ts=pos.entry_ts,
        exit_ts=exit_ts,
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
        holding_periods=pos.periods_held,
    )


# ---------------------------------------------------------------------------
# Shared action processing (used by backtest + live engines)
# ---------------------------------------------------------------------------


def _try_fill(
    action: Action, price: float, available_cash: float, cost_model: CostModel,
) -> tuple[Fill | None, float]:
    """Attempt a fill and validate cash sufficiency. Returns (fill, outlay) or (None, 0)."""
    fill = make_fill(action, price, available_cash, cost_model)
    if not fill or fill.quantity <= 0:
        return None, 0.0
    outlay = cost_model.estimate_entry_outlay(price, fill.quantity)
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
) -> ActionResults:
    """Process a bar's actions: open, scale, partial/full close.

    Shared by backtest and live engines to avoid logic duplication.
    Mutates *positions* dict in place. Returns trades, events, and cash delta.
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

        if action.type in ("buy", "sell"):
            desired_side: Literal["long", "short"] = "long" if action.type == "buy" else "short"

            if sym not in positions:
                # OPEN NEW
                fill, outlay = _try_fill(action, price, cash + cash_delta, cost_model)
                if fill:
                    cash_delta -= outlay
                    positions[sym] = PositionState(
                        symbol=sym,
                        side=fill.side,
                        entry_price=price,
                        quantity=fill.quantity,
                        entry_ts=ts,
                        periods_held=0,
                        entry_commission=fill.commission,
                        entry_slippage=fill.slippage,
                        total_entry_cost=price * fill.quantity * cost_model.multiplier,
                    )
                    events.append(OrderEvent(
                        ts=ts, symbol=sym, side=fill.side, event_type="open",
                        quantity=fill.quantity, price=price,
                        entry_price=price, position_quantity=fill.quantity,
                        notional=price * fill.quantity * cost_model.multiplier,
                        commission=fill.commission, slippage=fill.slippage,
                        tax=fill.tax, reason=reason,
                    ))

            elif positions[sym].side == desired_side:
                # SCALE IN — must specify quantity
                if action.quantity is None:
                    logger.debug("Scaling %s requires explicit quantity, skipping", sym)
                    continue
                fill, outlay = _try_fill(action, price, cash + cash_delta, cost_model)
                if fill:
                    cash_delta -= outlay
                    scale_into_position(positions[sym], fill, cost_model)
                    pos = positions[sym]
                    events.append(OrderEvent(
                        ts=ts, symbol=sym, side=pos.side, event_type="add",
                        quantity=fill.quantity, price=price,
                        entry_price=pos.entry_price, position_quantity=pos.quantity,
                        notional=price * fill.quantity * cost_model.multiplier,
                        commission=fill.commission, slippage=fill.slippage,
                        tax=fill.tax, reason=reason,
                    ))

            else:
                # OPPOSITE SIDE — reject
                logger.warning(
                    "Rejected %s %s: already %s — close first",
                    action.type, sym, positions[sym].side,
                )

        elif action.type == "close" and sym in positions:
            pos = positions[sym]
            close_qty = action.quantity

            # Reject zero-quantity close
            if close_qty is not None and close_qty <= 0:
                continue

            actual_close_qty = min(close_qty, pos.quantity) if close_qty else pos.quantity
            pnl, proceeds, fully_closed = close_position(
                pos, price, cost_model, quantity=close_qty,
            )
            trades.append(build_trade_result(pos, ts, price, actual_close_qty, pnl))
            cash_delta += proceeds

            remaining_qty = 0.0 if fully_closed else max(0.0, pos.quantity - actual_close_qty)
            events.append(OrderEvent(
                ts=ts, symbol=sym, side=pos.side,
                event_type="close" if fully_closed else "reduce",
                quantity=actual_close_qty, price=price,
                entry_price=pos.entry_price, position_quantity=remaining_qty,
                notional=price * actual_close_qty * cost_model.multiplier,
                commission=pnl.commission, slippage=pnl.slippage, tax=pnl.tax,
                pnl=pnl.net_pnl, net_return=pnl.net_return,
                entry_ts=pos.entry_ts, holding_periods=pos.periods_held,
                reason=reason,
            ))

            if fully_closed:
                del positions[sym]
            else:
                reduce_position(pos, actual_close_qty)

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)
