"""Execution layer — separates trade execution from engine logic.

BacktestExecutor: simulated fills using CostModel (for backtesting).
LiveExecutor: see librae/live_executor.py.

Position sizing is the strategy's responsibility (set Action.quantity).
If strategy doesn't specify quantity, executor uses all available cash.
"""
from __future__ import annotations

from typing import Protocol

from .cost_model import CostModel
from .strategy import Action, Fill

EPSILON = 1e-9


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


class BacktestExecutor:
    """Simulated execution using CostModel."""

    def __init__(self, cost_model: CostModel) -> None:
        self._cost_model = cost_model

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    def execute(self, action: Action, price: float, cash: float) -> Fill | None:
        if action.type == "hold":
            return None
        return make_fill(action, price, cash, self._cost_model)
