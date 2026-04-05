"""Unit tests for LiveTrader and LiveExecutor.

All tests use mocks — no real API calls, no DB, no Telegram.

Skills: python, quant
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import numpy as np
import pandas as pd
import pytest

from librae.core.cost_model import CostModel
from librae.live.executor import LiveExecutor
from librae.live.engine import LiveTrader
from librae.core.strategy import Action, BaseStrategy, Context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zero_cost_model() -> CostModel:
    return CostModel(
        multiplier=1.0, commission_rate=0.0, min_commission=0.0,
        slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
    )


def _make_ohlcv_df(n: int = 5, start_hour: int = 0) -> pd.DataFrame:
    """Create a simple OHLCV DataFrame with known timestamps."""
    base = datetime(2025, 1, 1, start_hour, 0, 0, tzinfo=timezone.utc)
    ts = pd.date_range(base, periods=n, freq="h", tz=timezone.utc)
    prices = np.arange(100.0, 100.0 + n, 1.0)
    return pd.DataFrame({
        "ts": ts,
        "open": prices - 0.5,
        "high": prices + 1.0,
        "low": prices - 1.0,
        "close": prices,
        "volume": np.full(n, 1000.0),
    })


def _simple_feature_fn(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Add entry_signal and exit_signal columns (all False by default)."""
    h1 = h1_base.copy()
    h1["entry_signal"] = False
    h1["exit_signal"] = False
    return h1


class _AlwaysBuyStrategy(BaseStrategy):
    """Buy if no position, close if has position."""

    def on_bar(self, ctx: Context) -> list[Action]:
        pos = ctx.positions.get(ctx.symbol)
        if pos:
            return [Action(type="close", symbol=ctx.symbol)]
        return [Action(type="buy", symbol=ctx.symbol, quantity=1.0)]


class _HoldStrategy(BaseStrategy):
    """Never trade."""

    def on_bar(self, ctx: Context) -> list[Action]:
        return []


# ---------------------------------------------------------------------------
# LiveExecutor tests
# ---------------------------------------------------------------------------

class TestLiveExecutor:

    def test_execute_buy_returns_fill(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=True)
        action = Action(type="buy", symbol="BTCUSDT", quantity=0.5)
        fill = executor.execute(action, price=100.0, cash=50_000.0)

        assert fill is not None
        assert fill.side == "long"
        assert fill.quantity == 0.5
        assert fill.price == 100.0

    def test_execute_sell_returns_short_fill(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=True)
        action = Action(type="sell", symbol="BTCUSDT", quantity=0.5)
        fill = executor.execute(action, price=100.0, cash=50_000.0)

        assert fill is not None
        assert fill.side == "short"

    def test_execute_hold_returns_none(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=True)
        action = Action(type="hold", symbol="BTCUSDT")
        assert executor.execute(action, 100.0, 50_000.0) is None

    def test_simulation_false_raises(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=False)
        action = Action(type="buy", symbol="BTCUSDT", quantity=1.0)
        with pytest.raises(NotImplementedError, match="Phase 4"):
            executor.execute(action, 100.0, 50_000.0)

    def test_quantity_none_uses_cash_sizing(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=True)
        action = Action(type="buy", symbol="BTCUSDT")
        fill = executor.execute(action, price=100.0, cash=500.0)

        assert fill is not None
        assert fill.quantity == pytest.approx(5.0, rel=1e-6)

    def test_zero_cash_returns_none(self):
        executor = LiveExecutor(_zero_cost_model(), simulation=True)
        action = Action(type="buy", symbol="BTCUSDT")
        fill = executor.execute(action, price=100.0, cash=0.0)

        assert fill is None

    def test_notify_exit_sends_telegram(self):
        mock_telegram = MagicMock()
        mock_telegram.enabled = True
        executor = LiveExecutor(
            _zero_cost_model(), simulation=True,
            telegram=mock_telegram, strategy_name="Test",
        )
        executor.notify_exit("BTCUSDT", 105.0)

        mock_telegram.send_signal.assert_called_once_with(
            strategy="Test", symbol="BTCUSDT", side="EXIT", price=105.0,
        )


# ---------------------------------------------------------------------------
# LiveTrader tests
# ---------------------------------------------------------------------------

class TestLiveTrader:

    def _make_runner(
        self,
        strategy: BaseStrategy | None = None,
        fetcher=None,
        feature_fn=None,
        executor: LiveExecutor | None = None,
        **kwargs,
    ) -> LiveTrader:
        return LiveTrader(
            strategy=strategy or _HoldStrategy(),
            symbols=["BTCUSDT"],
            fetcher=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
            feature_fn=feature_fn or _simple_feature_fn,
            executor=executor or LiveExecutor(_zero_cost_model(), simulation=True),
            timeframe="1h",
            warmup_bars=5,
            initial_balance=100_000.0,
            poll_seconds=0.0,
            **kwargs,
        )

    def test_max_iterations_stops(self):
        runner = self._make_runner()
        runner.run(max_iterations=2)
        # Should not hang — reaching here means it stopped

    def test_same_bar_not_processed_twice(self):
        """Strategy should only be called once for the same bar timestamp."""
        strategy = MagicMock(spec=BaseStrategy)
        strategy.on_bar.return_value = []

        runner = self._make_runner(strategy=strategy)
        runner.run(max_iterations=3)

        # First iteration detects the bar, subsequent ones see same ts → skip
        assert strategy.on_bar.call_count == 1

    def test_new_bar_triggers_strategy(self):
        """When fetcher returns a new timestamp, strategy is called again."""
        call_count = 0
        df1 = _make_ohlcv_df(n=5, start_hour=0)
        df2 = _make_ohlcv_df(n=5, start_hour=1)  # last ts is 1 hour later

        def fetcher(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return df1 if call_count <= 1 else df2

        strategy = MagicMock(spec=BaseStrategy)
        strategy.on_bar.return_value = []

        runner = self._make_runner(strategy=strategy, fetcher=fetcher)
        runner.run(max_iterations=2)

        assert strategy.on_bar.call_count == 2

    def test_ohlcv_cache_incremental_fetch(self):
        """After first full fetch, subsequent fetches use limit=2."""
        calls: list[dict] = []

        def tracking_fetcher(symbol: str, timeframe: str, limit: int, **kwargs):
            calls.append({"symbol": symbol, "limit": limit})
            return _make_ohlcv_df(n=limit, start_hour=len(calls))

        runner = self._make_runner(fetcher=tracking_fetcher)
        runner.run(max_iterations=3)

        assert calls[0]["limit"] == 5  # warmup_bars (full fetch)
        for c in calls[1:]:
            assert c["limit"] == 2  # incremental

    def test_bars_held_increments(self):
        """bars_held should increment each bar while position is open."""
        bars_held_values: list[int] = []

        class TrackBarsHeld(BaseStrategy):
            def on_bar(self, ctx: Context) -> list[Action]:
                pos = ctx.positions.get(ctx.symbol)
                if pos:
                    bars_held_values.append(pos.bars_held)
                    return []
                return [Action(type="buy", symbol=ctx.symbol, quantity=1.0)]

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(strategy=TrackBarsHeld(), fetcher=fetcher)
        runner.run(max_iterations=4)

        # Bar 0: buy (no position yet). Bar 1: held=1. Bar 2: held=2. Bar 3: held=3
        assert bars_held_values == [1, 2, 3]

    def test_close_calls_notify_exit(self):
        """Close action should call executor.notify_exit."""
        mock_executor = MagicMock(spec=LiveExecutor)
        mock_executor.cost_model = _zero_cost_model()
        mock_fill = MagicMock()
        mock_fill.side = "long"
        mock_fill.price = 100.0
        mock_fill.quantity = 1.0
        mock_executor.execute.return_value = mock_fill

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            executor=mock_executor,
            fetcher=fetcher,
        )
        runner.run(max_iterations=2)

        # Iteration 1: buy. Iteration 2: close (has position)
        mock_executor.notify_exit.assert_called_once()

    def test_cash_deducted_on_entry(self):
        """Cash should decrease after a buy."""
        cash_values: list[float] = []

        class TrackCash(BaseStrategy):
            def on_bar(self, ctx: Context) -> list[Action]:
                cash_values.append(ctx.cash)
                if not ctx.positions.get(ctx.symbol):
                    return [Action(type="buy", symbol=ctx.symbol, quantity=1.0)]
                return []

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(strategy=TrackCash(), fetcher=fetcher)
        runner.run(max_iterations=2)

        # First bar: full cash. Second bar: cash reduced by entry outlay
        assert cash_values[0] == 100_000.0
        assert cash_values[1] < 100_000.0
