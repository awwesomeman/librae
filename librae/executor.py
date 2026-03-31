"""Backward-compat shim — real module is librae.core.executor."""
from librae.core.executor import *  # noqa: F401,F403
from librae.core.executor import Executor, make_fill, size_position  # noqa: F811

# WHY: BacktestExecutor is deleted per plan D16, but keep a thin shim
# so existing tests pass during Part A. Will be removed in Part B.
from librae.core.cost_model import CostModel
from librae.core.strategy import Action, Fill


class BacktestExecutor:
    """Deprecated — use make_fill() directly. Kept for backward compat."""

    def __init__(self, cost_model: CostModel) -> None:
        self._cost_model = cost_model

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    def execute(self, action: Action, price: float, cash: float) -> Fill | None:
        if action.type == "hold":
            return None
        return make_fill(action, price, cash, self._cost_model)
