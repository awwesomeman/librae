"""Minimal SMA-crossover strategy — long-only, one signal in, one signal out.

prepare_signals() is called by both backtest (on the full history) and live
(on each new bar) via run.py, so the two can never compute the signal
differently — the same lesson as librae's own look-ahead-bias fix.
"""

from __future__ import annotations

import pandas as pd
from librae import Action, BaseStrategy, Context


def prepare_signals(df: pd.DataFrame, fast: int = 10, slow: int = 30) -> pd.DataFrame:
    """Add entry_signal/exit_signal columns from a fast/slow SMA cross."""
    out = df.copy()
    fast_sma = out["close"].rolling(fast).mean()
    slow_sma = out["close"].rolling(slow).mean()
    out["entry_signal"] = (fast_sma > slow_sma) & (fast_sma.shift(1) <= slow_sma.shift(1))
    out["exit_signal"] = (fast_sma < slow_sma) & (fast_sma.shift(1) >= slow_sma.shift(1))
    return out


class SmaCrossover(BaseStrategy):
    """Enter long on golden cross, close on death cross."""

    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.positions.get(ctx.symbol):
            if ctx.bar.get("exit_signal"):
                return [Action(type="close", symbol=ctx.symbol)]
            return []
        if ctx.bar.get("entry_signal"):
            return [Action(type="long", symbol=ctx.symbol, quantity=1.0)]
        return []
