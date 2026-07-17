"""TrendPullback strategy — BaseStrategy subclass.

Uses pre-computed entry/exit signal columns from ETL layer.
Strategy only does decision-making, not signal computation.
"""
from __future__ import annotations

from librae.core.strategy import Action, BaseStrategy, Context


class TrendPullbackStrategy(BaseStrategy):
    """TrendPullback: enter on pullback to EMA in uptrend, exit on trend break.

    Expects DataFrame to have these pre-computed columns:
    - entry_signal (bool): entry conditions met
    - exit_signal (bool): exit conditions met (close < ema20)

    Args:
        max_hold_periods: Force close after N periods in position.
    """

    def __init__(self, max_hold_periods: int = 24) -> None:
        self.max_hold_periods = max_hold_periods

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)

        if pos:
            if ctx.bar.get("exit_signal") or pos.periods_held >= self.max_hold_periods:
                return [Action(type="close", symbol=ctx.symbol)]
            return []

        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol)]

        return []
