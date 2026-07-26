"""Tests for the Backtest engine: Strategy protocol + Executor pattern."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.strategy import Action, BaseStrategy, Context

# ── Helpers ───────────────────────────────────────────────────────────────


def _make_multiindex_df(
    prices: list[float],
    symbol: str = "BTCUSDT",
) -> pd.DataFrame:
    """Create a MultiIndex (symbol, datetime) OHLCV DataFrame."""
    n = len(prices)
    close = np.array(prices, dtype=np.float64)
    dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    idx = pd.MultiIndex.from_arrays(
        [[symbol] * n, dt],
        names=["symbol", "datetime"],
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


def _zero_cost() -> CostModel:
    return CostModel.zero()


# ── Strategies for testing ───────────────────────────────────────────────


class HoldStrategy(BaseStrategy):
    """Never trades."""

    def on_bar(self, ctx: Context) -> list[Action]:
        return []


class BuyBar2CloseBar4(BaseStrategy):
    """Buy at bar 2, close at bar 4."""

    def on_bar(self, ctx: Context) -> list[Action]:
        if ctx.period_index == 2 and ctx.symbol not in ctx.positions:
            return [Action(type="long", symbol=ctx.symbol)]
        if ctx.period_index == 4 and ctx.symbol in ctx.positions:
            return [Action(type="close", symbol=ctx.symbol)]
        return []


class SignalDrivenStrategy(BaseStrategy):
    """Trades based on entry_signal / exit_signal columns in df."""

    def __init__(self, max_hold_periods: int = 24):
        self.max_hold_periods = max_hold_periods

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)
        if pos:
            if ctx.bar["exit_signal"] or pos.periods_held >= self.max_hold_periods:
                return [Action(type="close", symbol=ctx.symbol)]
        elif ctx.bar["entry_signal"]:
            return [Action(type="long", symbol=ctx.symbol)]
        return []


# ── Tests ─────────────────────────────────────────────────────────────────


class TestBacktestBasics:
    def test_no_trades_flat_equity(self) -> None:
        df = _make_multiindex_df([100.0] * 10)
        bt = Backtest(
            df, HoldStrategy(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        result = bt.run()

        assert len(result.trades) == 0
        assert np.isclose(result.final_equity, 10_000.0)
        assert len(result.equity_curve) == 10

    def test_single_round_trip(self) -> None:
        # WHY: next-bar execution — buy queued at bar 2, fills at bar 3 open.
        # Price must still be 100 at bar 3 for entry, 110 at bar 5 for exit.
        prices = [100.0, 100.0, 100.0, 100.0, 110.0, 110.0]
        df = _make_multiindex_df(prices)
        bt = Backtest(
            df,
            BuyBar2CloseBar4(),
            initial_balance=10_000,
            cost_model=_zero_cost(),
            data_source="test",
        )
        result = bt.run()

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert np.isclose(trade.entry_price, 100.0)
        assert np.isclose(trade.exit_price, 110.0)
        assert trade.gross_pnl > 0
        assert trade.symbol == "BTCUSDT"

    def test_force_close_at_end(self) -> None:
        prices = [100.0, 100.0, 100.0, 110.0, 120.0]
        df = _make_multiindex_df(prices)

        class BuyBar2(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.period_index == 2 and ctx.symbol not in ctx.positions:
                    return [Action(type="long", symbol=ctx.symbol)]
                return []

        bt = Backtest(
            df, BuyBar2(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        result = bt.run()

        assert len(result.trades) == 1
        assert np.isclose(result.trades[0].exit_price, 120.0)

    def test_requires_multiindex(self) -> None:
        df = pd.DataFrame({"close": [100.0]}, index=pd.date_range("2025-01-01", periods=1))
        with pytest.raises(ValueError, match="MultiIndex"):
            Backtest(df, HoldStrategy(), data_source="test")


class TestSignalDrivenStrategy:
    def test_entry_exit_signals(self) -> None:
        prices = [100.0] * 10
        df = _make_multiindex_df(prices)
        # Set entry at bar 3, exit at bar 6
        df.iloc[3, df.columns.get_loc("entry_signal")] = True
        df.iloc[6, df.columns.get_loc("exit_signal")] = True

        bt = Backtest(
            df,
            SignalDrivenStrategy(),
            initial_balance=10_000,
            cost_model=_zero_cost(),
            data_source="test",
        )
        result = bt.run()

        assert len(result.trades) == 1
        assert result.trades[0].periods_held == 3  # bar 4,5,6

    def test_max_hold_periods(self) -> None:
        prices = [100.0] * 20
        df = _make_multiindex_df(prices)
        df.iloc[2, df.columns.get_loc("entry_signal")] = True
        # No exit signal — should force close at max_hold_periods

        bt = Backtest(
            df,
            SignalDrivenStrategy(max_hold_periods=5),
            initial_balance=10_000,
            cost_model=_zero_cost(),
            data_source="test",
        )
        result = bt.run()

        assert len(result.trades) >= 1
        # WHY: next-bar execution adds 1 bar delay between close decision and fill
        assert result.trades[0].periods_held <= 6


class TestMultiAsset:
    def test_two_symbols(self) -> None:
        n = 10
        dt = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")

        rows = []
        for sym, base_price in [("AAA", 100.0), ("BBB", 200.0)]:
            for i in range(n):
                rows.append(
                    {
                        "symbol": sym,
                        "datetime": dt[i],
                        "open": base_price,
                        "high": base_price * 1.001,
                        "low": base_price * 0.999,
                        "close": base_price + i,  # trending up
                        "volume": 100.0,
                    }
                )

        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class BuyBothBar2(BaseStrategy):
            def on_bar(self, ctx):
                actions = []
                if ctx.period_index == 2:
                    n_symbols = len(ctx.symbols)
                    for sym in ctx.symbols:
                        if sym not in ctx.positions:
                            bar = ctx.bars.get(sym, {})
                            price = bar.get("close", 1.0)
                            qty = (ctx.cash / n_symbols) / price if price > 0 else 0
                            actions.append(Action(type="long", symbol=sym, quantity=qty))
                if ctx.period_index == 5:
                    for sym in ctx.symbols:
                        if sym in ctx.positions:
                            actions.append(Action(type="close", symbol=sym))
                return actions

        bt = Backtest(
            df, BuyBothBar2(), initial_balance=100_000, cost_model=_zero_cost(), data_source="test"
        )
        result = bt.run()

        assert len(result.trades) == 2
        symbols_traded = {t.symbol for t in result.trades}
        assert symbols_traded == {"AAA", "BBB"}

    def test_per_symbol_multiplier_resolved_independently_via_cfg(self) -> None:
        """Regression: a multi-asset cfg= run used to build exactly one
        CostModel from cfg.symbol (symbols[0]) and apply it to every symbol
        — TXFR1 (multiplier=200) and MXFR1 (multiplier=50) in the same
        tw_futures run would have silently shared TXFR1's multiplier."""
        from librae.core.run_config import RunConfig

        df = pd.concat(
            [
                _make_multiindex_df([100.0] * 5, symbol="TXFR1"),
                _make_multiindex_df([100.0] * 5, symbol="MXFR1"),
            ]
        )
        cfg = RunConfig(
            strategy_name="t",
            symbols=["TXFR1", "MXFR1"],
            timeframe="1h",
            market="tw_futures",
            data_source="shioaji",
            initial_balance=100_000.0,
            mode="backtest",
        )
        bt = Backtest(data=df, strategy=HoldStrategy(), cfg=cfg)
        assert bt._get_cost_model("TXFR1").multiplier == 200.0
        assert bt._get_cost_model("MXFR1").multiplier == 50.0

    def test_per_symbol_multiplier_via_symbol_overrides_no_yaml_edit_needed(self) -> None:
        """An unregistered symbol works via cfg.symbol_overrides alone —
        no symbols.py registry entry required."""
        from librae.core.run_config import RunConfig

        df = _make_multiindex_df([1.0] * 5, symbol="MY_CUSTOM_SYMBOL")
        cfg = RunConfig(
            strategy_name="t",
            symbols=["MY_CUSTOM_SYMBOL"],
            timeframe="1h",
            market="crypto",
            data_source="x",
            initial_balance=100_000.0,
            mode="backtest",
            symbol_overrides={"MY_CUSTOM_SYMBOL": {"multiplier": 1.0}},
        )
        bt = Backtest(data=df, strategy=HoldStrategy(), cfg=cfg)
        assert bt._get_cost_model("MY_CUSTOM_SYMBOL").multiplier == 1.0


class TestWithCosts:
    def test_commission_deducted(self) -> None:
        prices = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]
        df = _make_multiindex_df(prices)

        cost = CostModel(
            multiplier=1.0,
            commission_rate=0.01,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )

        bt = Backtest(
            df, BuyBar2CloseBar4(), initial_balance=10_000, cost_model=cost, data_source="test"
        )
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
                if ctx.period_index == 1:
                    return [Action(type="long", symbol=ctx.symbol)]
                if ctx.period_index == 4:
                    return [Action(type="close", symbol=ctx.symbol)]
                return []

        df = _make_multiindex_df([100.0] * 6)
        bt = Backtest(
            df, Spy(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        bt.run()

        # Bar 0-1: no position (buy queued at bar 1, not yet filled)
        assert len(seen_positions[0]) == 0
        assert len(seen_positions[1]) == 0
        # Bar 2-3: should see position (queued at bar 1, filled at bar 2)
        assert len(seen_positions[2]) == 1
        assert len(seen_positions[3]) == 1
        # Bar 4: still see position (close queued, fills at bar 5)
        assert len(seen_positions[4]) == 1

    def test_ctx_periods_held_increments(self) -> None:
        """periods_held in Position should increment each bar."""
        held_values: list[int] = []

        class Tracker(BaseStrategy):
            def on_bar(self, ctx):
                pos = ctx.positions.get(ctx.symbol)
                if pos:
                    held_values.append(pos.periods_held)
                if ctx.period_index == 1:
                    return [Action(type="long", symbol=ctx.symbol)]
                if ctx.period_index == 5:
                    return [Action(type="close", symbol=ctx.symbol)]
                return []

        df = _make_multiindex_df([100.0] * 8)
        bt = Backtest(
            df, Tracker(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        bt.run()

        # WHY: next-bar execution — buy queued at bar 1, fills at bar 2.
        # Bar 2 sees periods_held=0 (just entered), then increments each bar.
        assert held_values == [0, 1, 2, 3]
