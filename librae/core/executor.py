"""Execution layer — separates trade execution from engine logic.

Contains:
- Executor Protocol: interface for trade execution (live implements this)
- make_fill(): pure function for simulated fills (backtest uses directly)
- _size_position(): position sizing using all available cash
- calc_trade_pnl(): shared PnL calculation for backtest + live
- TradePnL: PnL breakdown dataclass

Position sizing is the strategy's responsibility (set Action.quantity).
If strategy doesn't specify quantity, executor uses all available cash.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from librae.core import EPSILON
from .cost_model import CostModel
from .strategy import Action, Fill, PositionState


class Executor(Protocol):
    """Interface for trade execution."""

    def execute(self, action: Action, price: float, cash: float) -> Fill | None:
        """Attempt to execute an Action at given price.

        Returns Fill if executed, None if rejected (e.g. insufficient cash).
        """
        ...


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
    holding_bars: int


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


def direction(side: Literal["long", "short"]) -> float:
    """Convert side to direction multiplier. +1 for long, -1 for short."""
    return -1.0 if side == "short" else 1.0


def calc_trade_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: Literal["long", "short"],
    cost_model: CostModel,
    entry_commission: float,
    entry_slippage: float,
) -> TradePnL:
    """Single trade PnL breakdown. Used by backtest + live.

    Args:
        entry_price: Price at position open.
        exit_price: Price at position close.
        quantity: Position size.
        side: "long" or "short".
        cost_model: CostModel for exit-side cost calculation.
        entry_commission: Entry-side commission (already paid).
        entry_slippage: Entry-side slippage (already paid).
    """
    dir_mult = direction(side)
    gross_pnl = cost_model.calc_pnl(entry_price, exit_price, quantity) * dir_mult

    exit_commission = cost_model.calc_commission(exit_price, quantity)
    exit_slippage = cost_model.calc_slippage(quantity)
    exit_tax = cost_model.calc_tax(exit_price, quantity, is_sell=True)

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


def close_position(
    pos: PositionState,
    exit_price: float,
    cost_model: CostModel,
) -> tuple[TradePnL, float]:
    """Close a position — shared by backtest and live engines.

    Returns (TradePnL, cash_proceeds).
    """
    pnl = calc_trade_pnl(
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=pos.quantity,
        side=pos.side,
        cost_model=cost_model,
        entry_commission=pos.entry_commission,
        entry_slippage=pos.entry_slippage,
    )
    notional = exit_price * pos.quantity * cost_model.multiplier
    proceeds = notional - pnl.exit_commission - pnl.exit_slippage - pnl.exit_tax
    return pnl, proceeds


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
