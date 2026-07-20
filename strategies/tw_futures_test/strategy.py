"""tw_futures_test — THROWAWAY always-flip strategy, not a real trading strategy.

Sole purpose: fires a signal on the very first bar it sees, guaranteeing an
order gets placed almost immediately, to drive an end-to-end
"strategy signal -> LiveTrader -> place_order" test against Shioaji's
sandbox. Delete once that test is done.
"""
from __future__ import annotations

from librae.core.strategy import Action, BaseStrategy, Context


class AlwaysFlipStrategy(BaseStrategy):
    """Buy 1 lot if flat, close if in position — fires a signal every bar."""

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)
        if pos:
            return [Action(type="close", symbol=ctx.symbol)]
        return [Action(type="long", symbol=ctx.symbol, quantity=1.0)]
