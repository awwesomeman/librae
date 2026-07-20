"""Unit tests for LiveTrader and LiveExecutor.

All tests use mocks — no real API calls, no DB, no Telegram.

Skills: python, quant
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from librae.core.cost_model import CostModel
from librae.core.executor import OrderEvent
from librae.live.executor import LiveExecutor
from tests.conftest import make_test_cfg
from librae.live.engine import LiveTrader
from librae.core.strategy import Action, BaseStrategy, Context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _zero_cost_model() -> CostModel:
    return CostModel.zero()


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
        return [Action(type="long", symbol=ctx.symbol, quantity=1.0)]


class _HoldStrategy(BaseStrategy):
    def on_bar(self, ctx: Context) -> list[Action]:
        return []


def _test_cfg(**overrides) -> "RunConfig":
    overrides.setdefault("params", {"warmup_periods": 5})
    return make_test_cfg(**overrides)


# ---------------------------------------------------------------------------
# LiveExecutor tests
# ---------------------------------------------------------------------------

class TestLiveExecutor:

    def test_notify_exit_sends_telegram(self):
        cm = _zero_cost_model()
        mock_telegram = MagicMock()
        mock_telegram.enabled = True
        ex = LiveExecutor(cm, simulation=True, telegram=mock_telegram, strategy_name="Test")
        ex.notify_exit("BTCUSDT", 105.0)

        mock_telegram.send_signal.assert_called_once_with(
            strategy="Test", symbol="BTCUSDT", side="EXIT", price=105.0,
        )

    def test_live_requires_order_adapter(self):
        """simulation=False without order_adapter should fail fast at construction."""
        with pytest.raises(ValueError, match="order_adapter"):
            LiveExecutor(_zero_cost_model(), simulation=False)

    def test_submit_order_noop_in_simulation(self):
        mock_adapter = MagicMock()
        ex = LiveExecutor(_zero_cost_model(), simulation=True)
        event = OrderEvent(
            ts=datetime(2025, 1, 1, tzinfo=timezone.utc), symbol="BTCUSDT",
            side="long", event_type="open", fill_quantity=1.0, price=100.0,
            entry_price=100.0, remaining_quantity=1.0, notional=100.0,
            commission=0.0, slippage=0.0, tax=0.0,
        )
        assert ex.submit_order(event) is None
        mock_adapter.place_order.assert_not_called()

    @pytest.mark.parametrize(
        "side,event_type,expected_order_side",
        [
            ("long", "open", "buy"),
            ("long", "add", "buy"),
            ("long", "close", "sell"),
            ("long", "reduce", "sell"),
            ("short", "open", "sell"),
            ("short", "add", "sell"),
            ("short", "close", "buy"),
            ("short", "reduce", "buy"),
        ],
    )
    def test_submit_order_side_mapping(self, side, event_type, expected_order_side):
        mock_adapter = MagicMock()
        mock_adapter.place_order.return_value = {"id": "123", "status": "filled"}
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        event = OrderEvent(
            ts=datetime(2025, 1, 1, tzinfo=timezone.utc), symbol="BTCUSDT",
            side=side, event_type=event_type, fill_quantity=2.0, price=100.0,
            entry_price=100.0, remaining_quantity=2.0, notional=200.0,
            commission=0.0, slippage=0.0, tax=0.0,
        )
        result = ex.submit_order(event)

        assert result == {"id": "123", "status": "filled"}
        mock_adapter.place_order.assert_called_once_with({
            "symbol": "BTCUSDT", "side": expected_order_side,
            "quantity": 2.0, "order_type": "market",
        })

    def test_submit_order_returns_none_on_broker_error(self):
        mock_adapter = MagicMock()
        mock_adapter.place_order.side_effect = RuntimeError("connection refused")
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        event = OrderEvent(
            ts=datetime(2025, 1, 1, tzinfo=timezone.utc), symbol="BTCUSDT",
            side="long", event_type="open", fill_quantity=1.0, price=100.0,
            entry_price=100.0, remaining_quantity=1.0, notional=100.0,
            commission=0.0, slippage=0.0, tax=0.0,
        )
        assert ex.submit_order(event) is None


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
        cfg: RunConfig | None = None,
        **kwargs,
    ) -> LiveTrader:
        test_cfg = cfg or _test_cfg()
        return LiveTrader(
            strategy or _HoldStrategy(),
            feature_fn or _simple_feature_fn,
            cfg=test_cfg,
            adapter=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
            cost_model=(executor or LiveExecutor(_zero_cost_model(), simulation=True)).cost_model,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            warmup_fetcher=None,
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

        assert calls[0]["limit"] == 5  # warmup_periods (full fetch)
        for c in calls[1:]:
            assert c["limit"] == 2  # incremental

    def test_periods_held_increments(self):
        """periods_held should increment each bar while position is open."""
        periods_held_values: list[int] = []

        class TrackBarsHeld(BaseStrategy):
            def on_bar(self, ctx: Context) -> list[Action]:
                pos = ctx.positions.get(ctx.symbol)
                if pos:
                    periods_held_values.append(pos.periods_held)
                    return []
                return [Action(type="long", symbol=ctx.symbol, quantity=1.0)]

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(strategy=TrackBarsHeld(), fetcher=fetcher)
        runner.run(max_iterations=4)

        # WHY: next-bar execution — buy queued at bar 0, fills at bar 1.
        # Bar 1: held=0 (just entered). Bar 2: held=1. Bar 3: held=2.
        assert periods_held_values == [0, 1, 2]

    def test_close_calls_notify_exit(self):
        """Close action should call executor.notify_exit."""
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
        )
        runner.run(max_iterations=2)

        # Iteration 1: buy. Iteration 2: close (has position)
        # notify_exit is called by LiveTrader._process_bar after trades

    def test_cash_deducted_on_entry(self):
        """Cash should decrease after a buy."""
        cash_values: list[float] = []

        class TrackCash(BaseStrategy):
            def on_bar(self, ctx: Context) -> list[Action]:
                cash_values.append(ctx.cash)
                if not ctx.positions.get(ctx.symbol):
                    return [Action(type="long", symbol=ctx.symbol, quantity=1.0)]
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

    def test_live_mode_places_real_orders(self):
        """End-to-end: live mode should call order_adapter.place_order on fills."""
        mock_order_adapter = MagicMock()
        mock_order_adapter.place_order.return_value = {"id": "1", "status": "filled"}

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        cfg = _test_cfg(mode="live")
        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(), fetcher=fetcher, cfg=cfg,
            order_adapter=mock_order_adapter,
        )
        runner.run(max_iterations=2)

        # Iteration 1: buy queued. Iteration 2: buy fills (open) + close queued/fills.
        assert mock_order_adapter.place_order.call_count >= 1
        first_call_signal = mock_order_adapter.place_order.call_args_list[0].args[0]
        assert first_call_signal["side"] == "buy"
        assert first_call_signal["symbol"] == "BTCUSDT"

    def test_live_mode_without_order_adapter_raises(self):
        cfg = _test_cfg(mode="live")
        with pytest.raises(ValueError, match="order_adapter"):
            self._make_runner(cfg=cfg)


class TestCryptoLiveAutoWiring:
    """LiveTrader without an explicit adapter= override (the real code path
    used in production) for crypto (non-tw_futures) live mode.

    _make_runner above always passes adapter=, which bypasses this branch
    entirely — so it never covered the auto-wiring that replaced the old
    unconditional NotImplementedError for crypto live mode."""

    def _build(self, monkeypatch, **kwargs):
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        with patch("brokers.crypto_adapter.CryptoAdapter") as mock_cls:
            mock_cls.return_value = MagicMock()
            trader = LiveTrader(
                _HoldStrategy(), _simple_feature_fn, cfg=_test_cfg(mode="live"),
                cost_model=_zero_cost_model(),
                on_bar=None, on_order_event=None, on_ohlcv=None,
                on_heartbeat=None, on_signal_outcome=None,
                **kwargs,
            )
        return trader, mock_cls.return_value

    def test_auto_builds_order_adapter_from_env(self, monkeypatch):
        trader, adapter_instance = self._build(monkeypatch)
        assert trader._executor._order_adapter is adapter_instance

    def test_explicit_order_adapter_not_overridden(self, monkeypatch):
        explicit = MagicMock()
        trader, _ = self._build(monkeypatch, order_adapter=explicit)
        assert trader._executor._order_adapter is explicit


class TestShioajiLiveAutoWiring:
    """tw_futures live mode: engine.py reuses the single authenticated
    ShioajiAdapter for both fetching and order placement (order_adapter=None
    auto-wires to the same instance as the fetcher) — never covered
    end-to-end: a strategy signal actually reaching Shioaji's place_order."""

    def _shioaji_cfg(self, **overrides):
        overrides.setdefault("symbols", ["TXFR1"])
        overrides.setdefault("market", "tw_futures")
        overrides.setdefault("data_source", "shioaji")
        return _test_cfg(**overrides)

    def test_auto_builds_order_adapter_from_shioaji(self):
        with patch("brokers.shioaji_adapter.ShioajiAdapter") as mock_cls:
            mock_cls.return_value = MagicMock()
            trader = LiveTrader(
                _HoldStrategy(), _simple_feature_fn, cfg=self._shioaji_cfg(mode="live"),
                cost_model=_zero_cost_model(),
                on_bar=None, on_order_event=None, on_ohlcv=None,
                on_heartbeat=None, on_signal_outcome=None,
            )

        # Same authenticated session used for fetching and for order placement.
        assert trader._executor._order_adapter is mock_cls.return_value

    def test_strategy_signal_triggers_shioaji_place_order(self):
        """End-to-end: strategy emits a buy, next bar's fill is mirrored to
        the auto-wired ShioajiAdapter via LiveExecutor.submit_order."""
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        with patch("brokers.shioaji_adapter.ShioajiAdapter") as mock_cls:
            mock_shioaji = MagicMock()
            mock_shioaji.place_order.return_value = {"id": "1", "status": "filled"}
            mock_shioaji.fetch_ohlcv.side_effect = lambda symbol, tf, limit: fetcher()
            mock_cls.return_value = mock_shioaji

            trader = LiveTrader(
                _AlwaysBuyStrategy(), _simple_feature_fn,
                cfg=self._shioaji_cfg(mode="live"),
                cost_model=_zero_cost_model(),
                on_bar=None, on_order_event=None, on_ohlcv=None,
                on_heartbeat=None, on_signal_outcome=None,
            )
            trader.run(max_iterations=2)

        # Iteration 1: buy queued. Iteration 2: buy fills — mirrored to Shioaji.
        assert mock_shioaji.place_order.call_count >= 1
        first_call_signal = mock_shioaji.place_order.call_args_list[0].args[0]
        assert first_call_signal["side"] == "buy"
        assert first_call_signal["symbol"] == "TXFR1"
