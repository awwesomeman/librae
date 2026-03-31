"""Execution layer — separates trade execution from engine logic.

Contains:
- Executor Protocol: interface for trade execution (live implements this)
- make_fill(): pure function for simulated fills (backtest uses directly)
- size_position(): position sizing using all available cash

Position sizing is the strategy's responsibility (set Action.quantity).
If strategy doesn't specify quantity, executor uses all available cash.
"""
from __future__ import annotations

from typing import Protocol

from librae.core import EPSILON
from .cost_model import CostModel
from .strategy import Action, Fill


class Executor(Protocol):
    """Interface for trade execution."""

    def execute(self, action: Action, price: float, cash: float) -> Fill | None:
        """Attempt to execute an Action at given price.

        Returns Fill if executed, None if rejected (e.g. insufficient cash).
        """
        ...


def size_position(cost_model: CostModel, price: float, cash: float) -> float:
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
        qty = size_position(cost_model, price, cash)
    if qty <= 0:
        return None

    return Fill(
        instrument=action.instrument,
        side="long" if action.type == "buy" else "short",
        price=price,
        quantity=qty,
        commission=cost_model.calc_commission(price, qty),
        slippage=cost_model.calc_slippage(qty),
        tax=cost_model.calc_tax(price, qty, is_sell=(action.type == "sell")),
    )
