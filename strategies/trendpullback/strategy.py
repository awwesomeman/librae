"""TrendPullback strategy — BaseStrategy subclass.

Uses pre-computed entry/exit signal columns from ETL layer.
Strategy only does decision-making, not signal computation.
"""
from __future__ import annotations

from librae.strategy import Action, BaseStrategy, Context


class TrendPullbackStrategy(BaseStrategy):
    """TrendPullback: enter on pullback to EMA in uptrend, exit on trend break.

    Expects DataFrame to have these pre-computed columns:
    - entry_signal (bool): entry conditions met
    - exit_signal (bool): exit conditions met (close < ema20)

    Args:
        max_hold_bars: Force close after N bars in position.
    """

    def __init__(self, max_hold_bars: int = 24) -> None:
        self.max_hold_bars = max_hold_bars

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.instrument)

        if pos:
            if ctx.bar.get("exit_signal") or pos.bars_held >= self.max_hold_bars:
                return [Action(type="close", instrument=ctx.instrument)]
            return []

        if ctx.bar.get("entry_signal"):
            return [Action(type="buy", instrument=ctx.instrument)]

        return []
