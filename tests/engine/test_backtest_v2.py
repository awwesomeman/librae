"""Tests for Backtest v2: Strategy protocol + Executor pattern."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from librae.cost_model import CostModel
from librae.engine import Backtest, BacktestResult
from librae.executor import BacktestExecutor
from librae.strategy import Action, BaseStrategy, Context


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_multiindex_df(
    prices: list[float],
    instrument: str = "BTCUSDT",
) -> pd.DataFrame:
    """Create a MultiIndex (instrument, datetime) OHLCV DataFrame."""
    n = len(prices)
    close = np.array(prices, dtype=np.float64)
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays(
        [[instrument] * n, dt], names=["instrument", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": np.full(n, 100.0),
            "entry_signal": False,
            "exit_signal": False,
        },
        index=idx,
    )


def _zero_cost_executor() -> BacktestExecutor:
    return BacktestExecutor(CostModel(
        multiplier=1.0, commission_rate=0.0, min_commission=0.0,
        slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
    ))


# ── Strategies for testing ───────────────────────────────────────────────


class HoldStrategy(BaseStrategy):
    """Never trades."""
    def on_bar(self, ctx: Context) -> list[Action]:
        return []


class BuyBar2CloseBar4(BaseStrategy):
    """Buy at bar 2, close at bar 4."""
    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.bar_index == 2 and ctx.instrument not in ctx.positions:
            return [Action(type="buy", instrument=ctx.instrument)]
        if ctx.bar_index == 4 and ctx.instrument in ctx.positions:
            return [Action(type="close", instrument=ctx.instrument)]
        return []


class SignalDrivenStrategy(BaseStrategy):
    """Trades based on entry_signal / exit_signal columns in df."""
    def __init__(self, max_hold_bars: int = 24):
        self.max_hold_bars = max_hold_bars

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.instrument)
        if pos:
            if ctx.bar["exit_signal"] or pos.bars_held >= self.max_hold_bars:
                return [Action(type="close", instrument=ctx.instrument)]
        elif ctx.bar["entry_signal"]:
            return [Action(type="buy", instrument=ctx.instrument)]
        return []


# ── Tests ─────────────────────────────────────────────────────────────────


class TestBacktestBasics:
    def test_no_trades_flat_equity(self) -> None:
        df = _make_multiindex_df([100.0] * 10)
        bt = Backtest(df, HoldStrategy(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 0
        assert np.isclose(result.final_equity, 10_000.0)
        assert len(result.equity_curve) == 10

    def test_single_round_trip(self) -> None:
        prices = [100.0, 100.0, 100.0, 110.0, 110.0, 110.0]
        df = _make_multiindex_df(prices)
        bt = Backtest(df, BuyBar2CloseBar4(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert np.isclose(trade.entry_price, 100.0)
        assert np.isclose(trade.exit_price, 110.0)
        assert trade.gross_pnl > 0
        assert trade.instrument == "BTCUSDT"

    def test_force_close_at_end(self) -> None:
        prices = [100.0, 100.0, 100.0, 110.0, 120.0]
        df = _make_multiindex_df(prices)

        class BuyBar2(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.bar_index == 2 and ctx.instrument not in ctx.positions:
                    return [Action(type="buy", instrument=ctx.instrument)]
                return []

        bt = Backtest(df, BuyBar2(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 1
        assert np.isclose(result.trades[0].exit_price, 120.0)

    def test_requires_multiindex(self) -> None:
        df = pd.DataFrame({"close": [100.0]}, index=pd.date_range("2025-01-01", periods=1))
        with pytest.raises(ValueError, match="MultiIndex"):
            Backtest(df, HoldStrategy())


class TestSignalDrivenStrategy:
    def test_entry_exit_signals(self) -> None:
        prices = [100.0] * 10
        df = _make_multiindex_df(prices)
        # Set entry at bar 3, exit at bar 6
        df.iloc[3, df.columns.get_loc("entry_signal")] = True
        df.iloc[6, df.columns.get_loc("exit_signal")] = True

        bt = Backtest(df, SignalDrivenStrategy(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) == 1
        assert result.trades[0].holding_bars == 3  # bar 4,5,6

    def test_max_hold_bars(self) -> None:
        prices = [100.0] * 20
        df = _make_multiindex_df(prices)
        df.iloc[2, df.columns.get_loc("entry_signal")] = True
        # No exit signal — should force close at max_hold_bars

        bt = Backtest(df, SignalDrivenStrategy(max_hold_bars=5),
                      initial_balance=10_000, executor=_zero_cost_executor())
        result = bt.run()

        assert len(result.trades) >= 1
        assert result.trades[0].holding_bars <= 5


class TestMultiAsset:
    def test_two_instruments(self) -> None:
        n = 10
        dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")

        rows = []
        for inst, base_price in [("AAA", 100.0), ("BBB", 200.0)]:
            for i in range(n):
                rows.append({
                    "instrument": inst,
                    "datetime": dt[i],
                    "open": base_price,
                    "high": base_price * 1.001,
                    "low": base_price * 0.999,
                    "close": base_price + i,  # trending up
                    "volume": 100.0,
                })

        df = pd.DataFrame(rows).set_index(["instrument", "datetime"])

        class BuyBothBar2(BaseStrategy):
            def on_bar(self, ctx):
                actions = []
                if ctx.bar_index == 2:
                    for inst in ctx.instruments:
                        if inst not in ctx.positions:
                            actions.append(Action(type="buy", instrument=inst))
                if ctx.bar_index == 5:
                    for inst in ctx.instruments:
                        if inst in ctx.positions:
                            actions.append(Action(type="close", instrument=inst))
                return actions

        executor = _zero_cost_executor()
        bt = Backtest(df, BuyBothBar2(), initial_balance=100_000, executor=executor)
        result = bt.run()

        assert len(result.trades) == 2
        instruments_traded = {t.instrument for t in result.trades}
        assert instruments_traded == {"AAA", "BBB"}


class TestWithCosts:
    def test_commission_deducted(self) -> None:
        prices = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        df = _make_multiindex_df(prices)

        cost_executor = BacktestExecutor(CostModel(
            multiplier=1.0, commission_rate=0.01, min_commission=0.0,
            slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
        ))

        bt = Backtest(df, BuyBar2CloseBar4(), initial_balance=10_000,
                      executor=cost_executor)
        result = bt.run()

        assert len(result.trades) == 1
        assert result.trades[0].commission > 0
        assert result.trades[0].net_pnl < result.trades[0].gross_pnl
        assert result.final_equity < 10_000  # lost money to commission


class TestContext:
    def test_ctx_has_positions(self) -> None:
        """Strategy can see positions in context after entry."""
        seen_positions: list[dict] = []

        class Spy(BaseStrategy):
            def on_bar(self, ctx):
                seen_positions.append(dict(ctx.positions))
                if ctx.bar_index == 1:
                    return [Action(type="buy", instrument=ctx.instrument)]
                if ctx.bar_index == 4:
                    return [Action(type="close", instrument=ctx.instrument)]
                return []

        df = _make_multiindex_df([100.0] * 6)
        bt = Backtest(df, Spy(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        bt.run()

        # Bar 0-1: no position
        assert len(seen_positions[0]) == 0
        assert len(seen_positions[1]) == 0
        # Bar 2-3: should see position (bought at bar 1)
        assert len(seen_positions[2]) == 1
        assert len(seen_positions[3]) == 1
        # Bar 4: still see position (close happens after on_bar)
        assert len(seen_positions[4]) == 1

    def test_ctx_bars_held_increments(self) -> None:
        """bars_held in Position should increment each bar."""
        held_values: list[int] = []

        class Tracker(BaseStrategy):
            def on_bar(self, ctx):
                pos = ctx.positions.get(ctx.instrument)
                if pos:
                    held_values.append(pos.bars_held)
                if ctx.bar_index == 1:
                    return [Action(type="buy", instrument=ctx.instrument)]
                if ctx.bar_index == 5:
                    return [Action(type="close", instrument=ctx.instrument)]
                return []

        df = _make_multiindex_df([100.0] * 8)
        bt = Backtest(df, Tracker(), initial_balance=10_000,
                      executor=_zero_cost_executor())
        bt.run()

        # bars_held should increment: 1, 2, 3, 4 (bars 2,3,4,5)
        assert held_values == [1, 2, 3, 4]
