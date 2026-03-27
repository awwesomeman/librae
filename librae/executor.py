"""Execution layer — separates trade execution from engine logic.

BacktestExecutor: simulated fills using CostModel (for backtesting).
LiveExecutor: real broker API (future, Phase 2-3).
"""
from __future__ import annotations

from typing import Protocol

from .cost_model import CostModel
from .strategy import Action, Fill

EPSILON = 1e-9
CASH_UTILIZATION = 0.95


class Executor(Protocol):
    """Interface for trade execution."""

    def execute(self, action: Action, price: float, cash: float) -> Fill | None:
        """Attempt to execute an Action at given price.

        Returns Fill if executed, None if rejected (e.g. insufficient cash).
        """
        ...


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

        cm = self._cost_model

        if action.type in ("buy", "sell"):
            qty = action.quantity
            if qty is None:
                qty = self._size_position(price, cash)
            if qty <= 0:
                return None

            commission = cm.calc_commission(price, qty)
            slippage = cm.calc_slippage(qty)
            tax = cm.calc_tax(price, qty, is_sell=(action.type == "sell"))

            return Fill(
                instrument=action.instrument,
                side="long" if action.type == "buy" else "short",
                price=price,
                quantity=qty,
                commission=commission,
                slippage=slippage,
                tax=tax,
            )

        # "close" actions are handled directly by the engine (it knows position size)
        return None

    def _size_position(self, price: float, cash: float) -> float:
        """Calculate position size that fits within available cash."""
        available = cash * CASH_UTILIZATION
        outlay_per_unit = self._cost_model.estimate_entry_outlay(price, 1.0)
        if outlay_per_unit < EPSILON:
            return 0.0
        return available / outlay_per_unit
