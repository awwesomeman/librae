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

# Canonical event_type="close"/"reduce" reasons — engine-generated (not
# strategy-chosen) exits use these exact strings so DB records stay queryable
# without free-text drift. Strategy-chosen closes may use any reason string.
REASON_STOP_LOSS = "stop_loss"
REASON_TAKE_PROFIT = "take_profit"
REASON_FORCE_CLOSE = "force_close"


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
        ts=ts, symbol=pos.symbol, side=pos.side,
        event_type="close" if fully_closed else "reduce",
        fill_quantity=close_qty, price=exit_price,
        entry_price=pos.entry_price, remaining_quantity=remaining_qty,
        notional=exit_price * close_qty * cost_model.multiplier,
        commission=pnl.commission, slippage=pnl.slippage, tax=pnl.tax,
        pnl=pnl.net_pnl, net_return=pnl.net_return,
        entry_at=pos.entry_at, periods_held=pos.periods_held,
        reason=reason,
    )
    return trade, event, proceeds, fully_closed


# ---------------------------------------------------------------------------
# Stop-loss / take-profit
# ---------------------------------------------------------------------------


def resolve_stop_exit(pos: PositionState, bar: dict[str, float]) -> tuple[float, str] | None:
    """Check whether this bar's range triggers pos's stop-loss or take-profit.

    stop_price is modeled as a stop-market order: once the bar's range
    reaches it, it fills at the *worse* of (stop_price, bar open) to capture
    gap-through risk. take_profit_price is modeled as a limit order: it
    fills exactly at that price once touched. Stop-loss is checked first —
    if both would trigger on the same bar, the conservative outcome wins.

    Returns (fill_price, reason) or None if neither is triggered.
    """
    high, low, open_ = bar.get("high"), bar.get("low"), bar.get("open")
    if high is None or low is None or open_ is None:
        return None
    is_long = pos.side == "long"

    if pos.stop_price is not None:
        triggered = low <= pos.stop_price if is_long else high >= pos.stop_price
        if triggered:
            fill = min(pos.stop_price, open_) if is_long else max(pos.stop_price, open_)
            return fill, REASON_STOP_LOSS

    if pos.take_profit_price is not None:
        triggered = high >= pos.take_profit_price if is_long else low <= pos.take_profit_price
        if triggered:
            return pos.take_profit_price, REASON_TAKE_PROFIT

    return None


def check_stop_targets(
    positions: dict[str, PositionState],
    bars: dict[str, dict[str, float]],
    ts: datetime,
    *,
    get_cost_model: Callable[[str], CostModel],
) -> ActionResults:
    """Force-close any position whose stop-loss/take-profit is hit this bar.

    Shared by backtest and live engines — called once per bar, before the
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
        hit = resolve_stop_exit(pos, bar)
        if hit is None:
            continue
        price, reason = hit
        cost_model = get_cost_model(sym)
        trade, event, proceeds, _ = build_close_event(pos, ts, price, cost_model, reason)
        trades.append(trade)
        events.append(event)
        cash_delta += proceeds
        del positions[sym]

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)


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


def _size_position(cost_model: CostModel, price: float, cash: float, side: Literal["long", "short"]) -> float:
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
    breakeven_qty = cost_model.min_commission / marginal_commission if marginal_commission > EPSILON else float("inf")
    if cash <= linear * breakeven_qty + cost_model.min_commission:
        qty = (cash - cost_model.min_commission) / linear
    else:
        qty = cash / (linear + marginal_commission)
    return max(qty, 0.0)


def make_fill(action: Action, price: float, cash: float, cost_model: CostModel) -> Fill | None:
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
        slippage=cost_model.calc_slippage(qty),
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
# Shared action processing (used by backtest + live engines)
# ---------------------------------------------------------------------------


def _try_fill(
    action: Action, price: float, available_cash: float, cost_model: CostModel,
) -> tuple[Fill | None, float]:
    """Attempt a fill and validate cash sufficiency. Returns (fill, outlay) or (None, 0)."""
    fill = make_fill(action, price, available_cash, cost_model)
    if not fill or fill.quantity <= 0:
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

        if action.type in ("long", "short"):
            desired_side: Literal["long", "short"] = action.type

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
                        entry_at=ts,
                        periods_held=0,
                        entry_commission=fill.commission,
                        entry_slippage=fill.slippage,
                        entry_tax=fill.tax,
                        total_entry_cost=price * fill.quantity * cost_model.multiplier,
                        stop_price=action.stop_price,
                        take_profit_price=action.take_profit_price,
                    )
                    events.append(OrderEvent(
                        ts=ts, symbol=sym, side=fill.side, event_type="open",
                        fill_quantity=fill.quantity, price=price,
                        entry_price=price, remaining_quantity=fill.quantity,
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
                    # WHY: re-issuing an add with a new stop/target lets a
                    # strategy trail its stop without a separate action type.
                    if action.stop_price is not None:
                        pos.stop_price = action.stop_price
                    if action.take_profit_price is not None:
                        pos.take_profit_price = action.take_profit_price
                    events.append(OrderEvent(
                        ts=ts, symbol=sym, side=pos.side, event_type="add",
                        fill_quantity=fill.quantity, price=price,
                        entry_price=pos.entry_price, remaining_quantity=pos.quantity,
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

            trade, event, proceeds, fully_closed = build_close_event(
                pos, ts, price, cost_model, reason, quantity=close_qty,
            )
            trades.append(trade)
            events.append(event)
            cash_delta += proceeds

            if fully_closed:
                del positions[sym]
            else:
                reduce_position(pos, trade.quantity)

    return ActionResults(trades=trades, events=events, cash_delta=cash_delta)


# ---------------------------------------------------------------------------
# Combined pending-fill + stop-check step (shared by backtest + live engines)
# ---------------------------------------------------------------------------


def run_pending_and_stops(
    ts: datetime,
    positions: dict[str, PositionState],
    cash: float,
    pending_actions: list[Action],
    bars: dict[str, dict[str, float]],
    *,
    get_cost_model: Callable[[str], CostModel],
    default_fill: str,
    primary_symbol: str,
) -> tuple[float, ActionResults]:
    """Fill this bar's pending actions, then check stop-loss/take-profit —
    the two steps that must always run together, in this order, before a
    strategy sees the bar. Combined into one function (previously hand-copied
    separately by the backtest and live engines) specifically because that
    duplication once let live's copy silently drop the stop-loss/take-profit
    step while backtest's kept it — a real bug, not hypothetical. Sharing
    this makes that class of drift structurally impossible.

    Returns (updated cash, combined ActionResults for both steps).
    """
    trades: list[TradeResult] = []
    events: list[OrderEvent] = []
    cash_delta_total = 0.0

    if pending_actions:
        fill_result = process_actions(
            pending_actions, positions, cash, ts,
            get_price=lambda sym, action: resolve_fill_price(
                bars.get(sym, {}), action, default_fill=default_fill),
            get_cost_model=get_cost_model,
            primary_symbol=primary_symbol,
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
