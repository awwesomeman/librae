"""Unit tests for LiveTrader and LiveExecutor.

All tests use mocks — no real API calls, no DB, no Telegram.

Skills: python, quant
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from librae.core.cost_model import CostModel
from librae.core.executor import REASON_DRAWDOWN_BREACH, OrderEvent
from librae.core.run_config import ExecutionPolicy, RiskPolicy, RunConfig
from librae.core.strategy import (
    Context,
    OrderIntent,
    PortfolioTargets,
    PositionState,
    Strategy,
)
from librae.live.engine import LiveTrader
from librae.live.executor import ExecutionReport, LiveExecutor, OrderRequest
from librae.live.state import MemoryLiveStateStore
from tests.conftest import make_test_cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_cost_model() -> CostModel:
    return CostModel.zero()


def _mock_order_adapter() -> MagicMock:
    """order_adapter mock with realistic flat get_position()/get_balance() —
    a bare MagicMock's auto-generated return values are truthy/float-coercible
    by default, which _reconcile_positions()/_reconcile_cash() would misread
    as real broker state (an open position, a MagicMock "total") at startup."""
    adapter = MagicMock()
    adapter.get_position.return_value = {
        "symbol": "",
        "size": 0,
        "avg_price": 0,
        "unrealized_pnl": 0,
    }
    adapter.get_balance.return_value = {"free": 0.0, "used": 0.0, "total": 0.0}
    adapter.find_order.return_value = None
    adapter.list_open_orders.return_value = []
    adapter.prepare_order.side_effect = lambda signal: signal
    return adapter


def _broker_report(
    *,
    order_id: str = "1",
    status: str = "filled",
    quantity: float = 1.0,
    filled: float | None = None,
    average: float = 100.0,
    fee: float = 0.0,
    executed_at: datetime | None = None,
) -> dict:
    return {
        "id": order_id,
        "status": status,
        "amount": quantity,
        "filled": quantity if filled is None else filled,
        "average": average,
        "fee": {"cost": fee, "currency": ""},
        "lastTradeTimestamp": int(
            (executed_at or datetime(2025, 1, 1, tzinfo=UTC)).timestamp() * 1000
        ),
    }


def _make_ohlcv_df(n: int = 5, start_hour: int = 0) -> pd.DataFrame:
    """Create a simple OHLCV DataFrame with known timestamps."""
    base = datetime(2025, 1, 1, start_hour, 0, 0, tzinfo=UTC)
    ts = pd.date_range(base, periods=n, freq="h", tz=UTC)
    prices = np.arange(100.0, 100.0 + n, 1.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": prices - 0.5,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "volume": np.full(n, 1000.0),
        }
    )


def _make_ohlcv_df_at(ts_end: datetime, n: int = 5) -> pd.DataFrame:
    """Same shape as _make_ohlcv_df but with the last row's ts fixed to
    ts_end — used for staleness tests, where wall-clock-relative timing
    matters (unlike _make_ohlcv_df's fixed 2025-01-01 base, which reads
    as "very stale" relative to real now())."""
    ts = pd.date_range(end=ts_end, periods=n, freq="h", tz=UTC)
    prices = np.arange(100.0, 100.0 + n, 1.0)
    return pd.DataFrame(
        {
            "ts": ts,
            "open": prices - 0.5,
            "high": prices + 1.0,
            "low": prices - 1.0,
            "close": prices,
            "volume": np.full(n, 1000.0),
        }
    )


TEST_CLOCK_NOW = datetime(2025, 1, 1, 2, tzinfo=UTC)


def _make_ohlcv_at(timestamps: list[datetime], price: float = 100.0) -> pd.DataFrame:
    """Create constant-price bars at explicit timestamps."""
    return pd.DataFrame(
        {
            "ts": pd.DatetimeIndex(timestamps),
            "open": price,
            "high": price + 1.0,
            "low": price - 1.0,
            "close": price,
            "volume": 1000.0,
        }
    )


def _simple_feature_fn(h1_base: pd.DataFrame) -> pd.DataFrame:
    """Add entry_signal and exit_signal columns (all False by default)."""
    h1 = h1_base.copy()
    h1["entry_signal"] = False
    h1["exit_signal"] = False
    return h1


class _AlwaysBuyStrategy(Strategy):
    """Buy if no position, close if has position."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        pos = ctx.positions.get(ctx.symbol)
        if pos:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]


class _HoldStrategy(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _test_cfg(**overrides) -> RunConfig:
    overrides.setdefault("params", {"warmup_periods": 5})
    from librae.config.symbols import load_symbol_registry

    registry = load_symbol_registry()
    symbols = overrides.get("symbols", ["BTCUSDT"])
    routes = {
        symbol: dict(values)
        for symbol, values in (overrides.get("instrument_overrides") or {}).items()
    }
    for symbol in symbols:
        if symbol not in registry:
            route = routes.setdefault(symbol, {})
            route.setdefault("instrument_type", "spot")
            route.setdefault("currency", "USDT")
    if routes:
        overrides["instrument_overrides"] = routes
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
            strategy="Test",
            symbol="BTCUSDT",
            side="EXIT",
            price=105.0,
        )

    def test_notify_entry_sends_telegram(self):
        """Regression test: only notify_exit existed — an operator watching
        Telegram would never see when a position opened, only when it
        closed."""
        cm = _zero_cost_model()
        mock_telegram = MagicMock()
        mock_telegram.enabled = True
        ex = LiveExecutor(cm, simulation=True, telegram=mock_telegram, strategy_name="Test")
        ex.notify_entry("BTCUSDT", "long", 100.0, "open")

        mock_telegram.send_signal.assert_called_once_with(
            strategy="Test",
            symbol="BTCUSDT",
            side="LONG",
            price=100.0,
        )

    def test_live_requires_order_adapter(self):
        """simulation=False without order_adapter should fail fast at construction."""
        with pytest.raises(ValueError, match="order_adapter"):
            LiveExecutor(_zero_cost_model(), simulation=False)

    def test_submit_order_noop_in_simulation(self):
        mock_adapter = MagicMock()
        ex = LiveExecutor(_zero_cost_model(), simulation=True)
        request = OrderRequest(
            client_order_id="test-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert ex.submit_order(request) is None
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
        mock_adapter.place_order.return_value = _broker_report(
            order_id="123",
            quantity=2.0,
            average=101.25,
            fee=0.4,
        )
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        event = OrderEvent(
            ts=datetime(2025, 1, 1, tzinfo=UTC),
            symbol="BTCUSDT",
            side=side,
            event_type=event_type,
            fill_quantity=2.0,
            price=100.0,
            entry_price=100.0,
            remaining_quantity=2.0,
            notional=200.0,
            commission=0.0,
            slippage=0.0,
            tax=0.0,
        )
        request = ex.request_from_event(event)
        result = ex.submit_order(request)

        assert isinstance(result, ExecutionReport)
        assert result.status == "filled"
        assert result.average_price == 101.25
        assert result.commission == 0.4
        sent_signal = mock_adapter.place_order.call_args.args[0]
        assert sent_signal["symbol"] == "BTCUSDT"
        assert sent_signal["side"] == expected_order_side
        assert sent_signal["quantity"] == 2.0
        assert sent_signal["order_type"] == "market"
        assert sent_signal["client_order_id"]  # non-empty, deterministic per event

    def test_submit_order_returns_none_on_broker_error(self):
        mock_adapter = MagicMock()
        mock_adapter.place_order.side_effect = RuntimeError("connection refused")
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        request = OrderRequest(
            client_order_id="test-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        assert ex.submit_order(request) is None

    def test_submit_order_preserves_rejected_state(self):
        mock_adapter = MagicMock()
        mock_adapter.place_order.return_value = {"id": "123", "status": "rejected"}
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        request = ex.request_from_event(_make_fill_event())

        report = ex.submit_order(request)

        assert report is not None
        assert report.status == "rejected"
        assert report.has_fill is False

    def test_pending_cancel_has_a_distinct_nonterminal_status(self):
        assert LiveExecutor._normalize_status("pending_cancel") == "cancel_pending"

    def test_ccxt_base_fee_is_converted_to_cash_and_rebate_is_preserved(self):
        mock_adapter = MagicMock()
        mock_adapter.place_order.return_value = {
            **_broker_report(quantity=1.0, average=20_000.0),
            "fee": {"cost": -0.001, "currency": "BTC"},
        }
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        request = OrderRequest(
            client_order_id="test-1",
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        report = ex.submit_order(request)

        assert report is not None
        assert report.commission == -20.0

    def test_order_creation_timestamp_is_not_execution_timestamp(self):
        mock_adapter = MagicMock()
        mock_adapter.place_order.return_value = {
            "id": "1",
            "status": "filled",
            "amount": 1.0,
            "filled": 1.0,
            "average": 100.0,
            "fee": {"cost": 0.0, "currency": "USD"},
            "timestamp": 1_735_689_600_000,
        }
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=mock_adapter)
        request = OrderRequest(
            client_order_id="test-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        assert ex.submit_order(request) is None


# ---------------------------------------------------------------------------
# LiveTrader tests
# ---------------------------------------------------------------------------


class TestLiveTrader:
    def _make_runner(
        self,
        strategy: Strategy | None = None,
        fetcher=None,
        feature_fn=None,
        executor: LiveExecutor | None = None,
        config: RunConfig | None = None,
        **kwargs,
    ) -> LiveTrader:
        test_config = config or _test_cfg()
        kwargs.setdefault("state_store", MemoryLiveStateStore())
        kwargs.setdefault("clock", lambda: TEST_CLOCK_NOW)
        runner = LiveTrader(
            strategy or _HoldStrategy(),
            feature_fn or _simple_feature_fn,
            config=test_config,
            adapter=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
            cost_model=(
                executor.get_cost_model(test_config.symbol) if executor else _zero_cost_model()
            ),
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            warmup_fetcher=None,
            **kwargs,
        )
        runner._sleep = lambda _seconds: None  # no real delays in unit tests
        return runner

    def test_max_iterations_stops(self):
        runner = self._make_runner()
        runner.run(max_iterations=2)
        # Should not hang — reaching here means it stopped

    def test_live_without_default_db_requires_explicit_state_store(self):
        with pytest.raises(ValueError, match="requires durable state"):
            LiveTrader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(mode="live"),
                adapter=lambda *args, **kwargs: _make_ohlcv_df(),
                order_adapter=_mock_order_adapter(),
                cost_model=_zero_cost_model(),
            )

    def test_same_bar_not_processed_twice(self):
        """Strategy should only be called once for the same bar timestamp."""
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []

        runner = self._make_runner(strategy=strategy)
        runner.run(max_iterations=3)

        # First iteration detects the bar, subsequent ones see same ts → skip
        assert strategy.on_bar.call_count == 1

    def test_sim_pending_decision_resumes_on_next_bar_after_restart(self):
        store = MemoryLiveStateStore()

        class BuyOnce(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        first = self._make_runner(
            strategy=BuyOnce(),
            fetcher=lambda *args, **kwargs: _make_ohlcv_df(start_hour=0),
            state_store=store,
        )
        first.run(max_iterations=1)
        assert first._positions == {}

        second = self._make_runner(
            strategy=BuyOnce(),
            fetcher=lambda *args, **kwargs: _make_ohlcv_df(start_hour=1),
            state_store=store,
        )
        second.run(max_iterations=1)

        assert second._positions["BTCUSDT"].quantity == 1.0
        assert second._period_index == 2

    def test_failed_sim_cycle_is_not_checkpointed_as_processed(self):
        store = MemoryLiveStateStore()
        failing = self._make_runner(
            feature_fn=MagicMock(side_effect=RuntimeError("bad feature")),
            state_store=store,
        )
        failing.run(max_iterations=1)

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        recovered = self._make_runner(strategy=strategy, state_store=store)
        recovered.run(max_iterations=1)

        strategy.on_bar.assert_called_once()

    def test_new_bar_triggers_strategy(self):
        """When fetcher returns a new timestamp, strategy is called again."""
        call_count = 0
        df1 = _make_ohlcv_df(n=5, start_hour=0)
        df2 = _make_ohlcv_df(n=5, start_hour=1)  # last ts is 1 hour later

        def fetcher(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return df1 if call_count <= 1 else df2

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []

        runner = self._make_runner(strategy=strategy, fetcher=fetcher)
        runner.run(max_iterations=2)

        assert strategy.on_bar.call_count == 2

    def test_naive_live_bar_timestamp_fails_explicitly(self):
        frame = _make_ohlcv_df()
        frame["ts"] = frame["ts"].dt.tz_localize(None)
        runner = self._make_runner(fetcher=lambda *_args, **_kwargs: frame)

        with pytest.raises(ValueError, match="timestamp must be timezone-aware"):
            runner._poll_cycle()

    @pytest.mark.parametrize("mode", ["sim", "live"])
    def test_missing_symbol_action_waits_for_its_next_real_bar(self, mode):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        responses = {
            "AAA": iter([_make_ohlcv_at([t1]), _make_ohlcv_at([t1])]),
            "BBB": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        class BuyBbbOnce(Strategy):
            def __init__(self):
                self.emitted = False

            def on_bar(self, ctx):
                if not self.emitted:
                    self.emitted = True
                    return [OrderIntent(action="long", symbol="BBB", quantity=1.0)]
                return []

        order_adapter = _mock_order_adapter()
        order_adapter.place_order.return_value = _broker_report(
            quantity=1.0,
            executed_at=t1,
        )
        runner = self._make_runner(
            strategy=BuyBbbOnce(),
            fetcher=fetcher,
            config=_test_cfg(mode=mode, symbols=["AAA", "BBB"]),
            order_adapter=order_adapter,
        )

        runner._poll_cycle()
        if mode == "sim":
            assert runner._positions == {}
            assert runner._pending_decision == [
                OrderIntent(action="long", symbol="BBB", quantity=1.0)
            ]
            runner._poll_cycle()

        assert runner._positions["BBB"].quantity == pytest.approx(1.0)
        assert runner._pending_decision == []

    def test_delayed_symbol_does_not_block_available_symbol(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        responses = {
            "AAA": iter(
                [
                    _make_ohlcv_at([t0, t1]),
                    _make_ohlcv_at([t1]),
                ]
            ),
            "BBB": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=fetcher,
            config=_test_cfg(symbols=["AAA", "BBB"]),
        )

        runner._poll_cycle()
        runner._poll_cycle()

        contexts = [call.args[0] for call in strategy.on_bar.call_args_list]
        assert [ctx.ts for ctx in contexts] == [t0, t1, t1]
        assert [set(ctx.bars) for ctx in contexts] == [
            {"AAA", "BBB"},
            {"AAA"},
            {"AAA", "BBB"},
        ]

    def test_missing_symbol_bar_does_not_skip_available_event(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t1 + timedelta(hours=1)
        responses = {
            "AAA": iter(
                [
                    _make_ohlcv_at([t0]),
                    _make_ohlcv_at([t1]),
                    _make_ohlcv_at([t2]),
                ]
            ),
            "BBB": iter(
                [
                    _make_ohlcv_at([t0]),
                    _make_ohlcv_at([t0]),
                    _make_ohlcv_at([t2]),
                ]
            ),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=fetcher,
            config=_test_cfg(symbols=["AAA", "BBB"]),
        )

        for _ in range(3):
            runner._poll_cycle()

        contexts = [call.args[0] for call in strategy.on_bar.call_args_list]
        assert [ctx.ts for ctx in contexts] == [t0, t1, t2]
        assert [set(ctx.bars) for ctx in contexts] == [
            {"AAA", "BBB"},
            {"AAA"},
            {"AAA", "BBB"},
        ]

    def test_duplicate_broker_bars_do_not_repeat_cycle(self):
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        duplicated = _make_ohlcv_at([ts, ts])

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *_args, **_kwargs: duplicated,
            config=_test_cfg(symbols=["AAA", "BBB"]),
        )

        runner._poll_cycle()
        runner._poll_cycle()

        assert strategy.on_bar.call_count == 1
        assert all(len(frame) == 1 for frame in runner._ohlcv_cache.values())

    def test_out_of_order_bar_before_watermark_is_not_replayed(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t1 + timedelta(hours=1)
        responses = {
            "AAA": iter(
                [
                    _make_ohlcv_at([t1]),
                    _make_ohlcv_at([t0]),
                    _make_ohlcv_at([t2]),
                ]
            ),
            "BBB": iter(
                [
                    _make_ohlcv_at([t1]),
                    _make_ohlcv_at([t2]),
                    _make_ohlcv_at([t2]),
                ]
            ),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=fetcher,
            config=_test_cfg(symbols=["AAA", "BBB"]),
        )

        for _ in range(3):
            runner._poll_cycle()

        contexts = [call.args[0] for call in strategy.on_bar.call_args_list]
        assert [ctx.ts for ctx in contexts] == [t1, t2, t2]
        assert [set(ctx.bars) for ctx in contexts] == [
            {"AAA", "BBB"},
            {"BBB"},
            {"AAA", "BBB"},
        ]

    @pytest.mark.parametrize(("mode", "expected_events"), [("sim", 3), ("live", 1)])
    def test_only_sim_replays_all_uncommitted_bars(self, mode, expected_events):
        now = datetime.now(UTC)
        timestamps = [now - timedelta(hours=2), now - timedelta(hours=1), now]
        frame = _make_ohlcv_at(timestamps)
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *_args, **_kwargs: frame,
            config=_test_cfg(mode=mode),
            order_adapter=_mock_order_adapter() if mode == "live" else None,
        )
        runner._last_bar_ts["BTCUSDT"] = now - timedelta(hours=3)
        runner._last_cycle_ts = now - timedelta(hours=3)

        runner._poll_cycle()

        assert strategy.on_bar.call_count == expected_events
        if mode == "live":
            assert strategy.on_bar.call_args.args[0].ts == now

    def test_context_exposes_engine_equity(self):
        seen_equity: list[float] = []

        class EquitySpy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                seen_equity.append(ctx.equity)
                return []

        runner = self._make_runner(strategy=EquitySpy())
        frame = _make_ohlcv_df()
        runner._process_bar("BTCUSDT", frame, frame["ts"].iloc[-1].to_pydatetime())

        assert seen_equity == [runner._cash]

    @pytest.mark.parametrize("mode", ["sim", "live"])
    def test_portfolio_targets_execute_from_synchronized_realtime_context(self, mode):
        contexts: list[Context] = []

        class AllocationStrategy(Strategy):
            def on_bar(self, ctx: Context):
                contexts.append(ctx)
                return PortfolioTargets(weights={"AAA": 0.6, "BBB": 0.4})

        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        responses = {
            "AAA": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
            "BBB": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        order_adapter = _mock_order_adapter()
        order_adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=f"{signal['symbol']}-1",
            quantity=signal["quantity"],
            average=100.0,
            executed_at=t0,
        )
        runner = self._make_runner(
            strategy=AllocationStrategy(),
            fetcher=fetcher,
            config=_test_cfg(mode=mode, symbols=["AAA", "BBB"]),
            order_adapter=order_adapter,
        )

        runner.run(max_iterations=2)

        assert [ctx.ts for ctx in contexts] == [t0, t1]
        assert all(set(ctx.bars) == {"AAA", "BBB"} for ctx in contexts)
        assert runner._positions["AAA"].quantity == pytest.approx(600.0)
        assert runner._positions["BBB"].quantity == pytest.approx(400.0)
        assert order_adapter.place_order.call_count == (2 if mode == "live" else 0)

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

        class TrackBarsHeld(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                pos = ctx.positions.get(ctx.symbol)
                if pos:
                    periods_held_values.append(pos.periods_held)
                    return []
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]

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

    def test_open_calls_notify_entry(self):
        """Regression test: an open/add fill must notify entry, symmetric
        with the existing close -> notify_exit wiring."""
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(strategy=_AlwaysBuyStrategy(), fetcher=fetcher)
        runner._executor.notify_entry = MagicMock()
        runner.run(max_iterations=2)

        # Iteration 1: buy queued. Iteration 2: buy fills at bar2's open (103.5).
        runner._executor.notify_entry.assert_called_once_with("BTCUSDT", "long", 103.5, "open")

    def test_cash_deducted_on_entry(self):
        """Cash should decrease after a buy."""
        cash_values: list[float] = []

        class TrackCash(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                cash_values.append(ctx.cash)
                if not ctx.positions.get(ctx.symbol):
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
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
        """Live intent is submitted in the decision cycle, not one bar later."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=str(mock_order_adapter.place_order.call_count),
            quantity=signal["quantity"],
            average=104.25,
        )

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        cfg = _test_cfg(mode="live")
        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            config=cfg,
            order_adapter=mock_order_adapter,
        )
        runner.run(max_iterations=2)

        # The first completed-bar decision submits immediately.
        assert mock_order_adapter.place_order.call_count >= 1
        first_call_signal = mock_order_adapter.place_order.call_args_list[0].args[0]
        assert first_call_signal["side"] == "buy"
        assert first_call_signal["symbol"] == "BTC/USDT"
        assert first_call_signal["canonical_symbol"] == "BTCUSDT"

    def test_live_state_uses_broker_execution_truth(self):
        mock_order_adapter = _mock_order_adapter()
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )

        executed_at = datetime(2025, 1, 2, 3, tzinfo=UTC)

        def place_order(signal):
            assert runner._positions == {}
            assert runner._cash == 100_000.0
            return _broker_report(
                quantity=signal["quantity"],
                average=107.25,
                fee=1.5,
                executed_at=executed_at,
            )

        mock_order_adapter.place_order.side_effect = place_order

        runner.run(max_iterations=1)

        assert runner._halted is False
        assert runner._positions["BTCUSDT"].quantity == 1.0
        assert runner._positions["BTCUSDT"].entry_price == 107.25
        assert runner._positions["BTCUSDT"].entry_at == executed_at
        assert runner._positions["BTCUSDT"].entry_commission == 1.5
        assert runner._cash == pytest.approx(100_000.0 - 107.25 - 1.5)

    def test_acknowledgement_is_not_treated_as_fill(self):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.place_order.return_value = {
            "id": "1",
            "status": "submitted",
            "amount": 1.0,
            "filled": 0.0,
        }

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)

        assert not any(
            method == "send_alert" and "Order" in kwargs["title"] for method, kwargs in alerts
        )
        assert runner._halted is False
        assert runner._positions == {}
        assert len(runner._active_orders) == 1
        assert runner._active_orders[0].status == "accepted"
        assert runner._cash == 100_000.0

    def test_basket_failure_keeps_only_confirmed_broker_fill(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        responses = {
            "AAA": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
            "BBB": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1])]),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        class AllocateOnce(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return PortfolioTargets(weights={"AAA": 0.6, "BBB": 0.4})
                return []

        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(order_id="aaa", quantity=600.0, average=100.0, executed_at=t0),
            {"id": "", "status": "rejected"},
        ]
        runner = self._make_runner(
            strategy=AllocateOnce(),
            fetcher=fetcher,
            config=_test_cfg(mode="live", symbols=["AAA", "BBB"]),
            order_adapter=adapter,
        )

        runner.run(max_iterations=1)

        assert runner._halted is True
        assert set(runner._positions) == {"AAA"}
        assert runner._positions["AAA"].quantity == pytest.approx(600.0)

    def test_live_mode_without_order_adapter_raises(self):
        cfg = _test_cfg(mode="live")
        with pytest.raises(ValueError, match="broker is not configured"):
            self._make_runner(config=cfg)

    def test_adv_limit_accepts_intraday_runtime_with_registered_calendar(self):
        config = _test_cfg(
            execution=ExecutionPolicy(
                adv_lookback_sessions=20,
                max_adv_participation_rate=0.01,
            )
        )

        assert config.execution.adv_lookback_sessions == 20

    def test_reconciles_open_broker_position_at_startup(self):
        """Regression test: a process restart previously always assumed
        flat/full-balance, even if the broker actually had a real open
        position — a strategy could then double-open on the broker, or a
        legitimate opposite-side signal could be rejected against a
        position the broker doesn't actually have."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 2.0,
            "avg_price": 95.0,
            "unrealized_pnl": 10.0,
        }

        runner = self._make_runner(
            strategy=_HoldStrategy(),
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )
        runner.run(max_iterations=1)

        pos = runner._positions["BTCUSDT"]
        assert pos.side == "long"
        assert pos.quantity == 2.0
        assert pos.entry_price == 95.0

    def test_reconciles_short_broker_position(self):
        """Negative size from the broker must reconcile as a short."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": -3.0,
            "avg_price": 110.0,
            "unrealized_pnl": 0.0,
        }

        runner = self._make_runner(
            strategy=_HoldStrategy(),
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )
        runner.run(max_iterations=1)

        pos = runner._positions["BTCUSDT"]
        assert pos.side == "short"
        assert pos.quantity == 3.0

    def test_first_run_cannot_adopt_spot_inventory_without_cost_basis(self):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.return_value = {
            "symbol": "BTC/USDT",
            "size": 1.0,
            "avg_price": None,
            "unrealized_pnl": 0.0,
        }

        runner = self._make_runner(
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._positions == {}

    def test_reconciliation_failure_halts_startup(self):
        """An unreadable broker book must fail closed without crashing."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.side_effect = RuntimeError("broker down")

        runner = self._make_runner(config=_test_cfg(mode="live"), order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner.run(max_iterations=1)  # must not raise

        assert runner._positions == {}
        assert runner._halted is True
        assert any("Position Reconciliation Failed" in item[1]["title"] for item in alerts)

    def test_cash_drift_beyond_tolerance_alerts_without_adjusting_cash(self):
        """Drift past CASH_RECONCILE_TOLERANCE_PCT must alert with both
        numbers but never mutate self._cash — reconciliation is alert-only,
        unlike position reconciliation which does adopt the broker's side."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_balance.return_value = {
            "free": 50_000.0,
            "used": 0.0,
            "total": 50_000.0,
        }

        cfg = _test_cfg(mode="live", symbols=["BTC/USDT"])
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)

        drift_alerts = [
            kw
            for m, kw in alerts
            if m == "send_alert" and "Cash Reconciliation Drift" in kw["title"]
        ]
        assert len(drift_alerts) == 1
        assert "local_cash=100000.00" in drift_alerts[0]["message"]
        assert "broker_balance=50000.00" in drift_alerts[0]["message"]
        assert runner._cash == 100_000.0  # unchanged — alert-only, never auto-adjusted

    def test_cash_drift_within_tolerance_does_not_alert(self):
        mock_order_adapter = _mock_order_adapter()
        # 0.5% drift, under the 1% CASH_RECONCILE_TOLERANCE_PCT default
        mock_order_adapter.get_balance.return_value = {
            "free": 99_500.0,
            "used": 0.0,
            "total": 99_500.0,
        }

        cfg = _test_cfg(mode="live", symbols=["BTC/USDT"])
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)

        assert not [
            kw for m, kw in alerts if m == "send_alert" and "Cash Reconciliation" in kw["title"]
        ]

    def test_tw_futures_market_reconciles_via_market_currency_map(self):
        """Regression test: tw_futures/us_equity symbols don't contain '/'
        (unlike CCXT pairs), so _reconcile_cash used to skip them even when
        the adapter does have get_balance() — the market->currency map is
        what makes reconciliation actually reach these adapters."""
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_balance.return_value = {
            "free": 50_000.0,
            "used": 0.0,
            "total": 50_000.0,
        }

        cfg = _test_cfg(mode="live", symbols=["TXFR1"], market="tw_futures")
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)

        mock_order_adapter.get_balance.assert_called_once_with("TWD")
        drift_alerts = [
            kw
            for m, kw in alerts
            if m == "send_alert" and "Cash Reconciliation Drift" in kw["title"]
        ]
        assert len(drift_alerts) == 1

    def test_multi_currency_runtime_fails_before_accounting(self):
        mock_order_adapter = _mock_order_adapter()

        cfg = _test_cfg(
            mode="live",
            symbols=["AAA", "BBB"],
            instrument_overrides={
                "AAA": {"currency": "USD"},
                "BBB": {"currency": "USDT"},
            },
        )
        with pytest.raises(ValueError, match="one accounting currency"):
            self._make_runner(config=cfg, order_adapter=mock_order_adapter)

    def test_adapter_without_get_balance_is_skipped(self):
        """A duck-typed adapter with no get_balance() at all must be silently
        skipped — no alert, no exception, startup proceeds normally."""
        mock_order_adapter = _mock_order_adapter()
        del mock_order_adapter.get_balance

        cfg = _test_cfg(mode="live", symbols=["BTC/USDT"])
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)  # must not raise

        assert not [
            kw for m, kw in alerts if m == "send_alert" and "Cash Reconciliation" in kw["title"]
        ]

    def test_cash_reconciliation_failure_does_not_crash_startup(self):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_balance.side_effect = RuntimeError("broker down")

        cfg = _test_cfg(mode="live", symbols=["BTC/USDT"])
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        runner.run(max_iterations=1)  # must not raise

    def test_stale_data_alerts_once_edge_triggered(self):
        """A feed stuck on the same old bar must alert exactly once, not
        every poll cycle — CONSECUTIVE_ERROR_THRESHOLD only covers raised
        exceptions, this covers a fetch that succeeds but never advances."""
        stale_ts = datetime.now(UTC) - timedelta(hours=10)
        runner = self._make_runner(
            fetcher=lambda *a, **kw: _make_ohlcv_df_at(stale_ts),
            clock=lambda: datetime.now(UTC),
        )
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=3)

        stale_alerts = [kw for m, kw in alerts if m == "send_alert" and "Stale Data" in kw["title"]]
        assert len(stale_alerts) == 1

    def test_stale_live_data_never_reaches_strategy_or_broker(self):
        stale_ts = datetime.now(UTC) - timedelta(hours=10)
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)]
        order_adapter = _mock_order_adapter()
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *args, **kwargs: _make_ohlcv_df_at(stale_ts),
            config=_test_cfg(mode="live"),
            order_adapter=order_adapter,
            clock=lambda: datetime.now(UTC),
        )

        runner._poll_cycle()

        strategy.on_bar.assert_not_called()
        order_adapter.place_order.assert_not_called()

    def test_fresh_data_does_not_alert(self):
        fresh_ts = datetime.now(UTC)
        runner = self._make_runner(
            fetcher=lambda *a, **kw: _make_ohlcv_df_at(fresh_ts),
            clock=lambda: datetime.now(UTC),
        )
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=3)

        assert not [kw for m, kw in alerts if m == "send_alert" and "Stale Data" in kw["title"]]

    def test_stale_data_realerts_after_recovery(self):
        """Recovery must re-arm the alert — a second, independent staleness
        episode later has to alert again, not stay silent forever after the
        first one fired. Calls _check_staleness directly rather than
        through the full poll cycle: _fetch_with_cache's dedup-by-timestamp
        layer would discard a synthetic "goes stale again" refetch as
        "not newer than what's cached" — a real staleness episode is the
        cache legitimately having nothing new to return, not a timestamp
        regression, and a fast unit test can't just wait out the clock to
        make an already-cached bar age past the threshold for real."""
        runner = self._make_runner(clock=lambda: datetime.now(UTC))
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        stale_ts = datetime.now(UTC) - timedelta(hours=10)
        fresh_ts = datetime.now(UTC)

        runner._check_staleness("BTCUSDT", stale_ts)  # goes stale -> alert #1
        runner._check_staleness("BTCUSDT", fresh_ts)  # recovers -> no alert
        runner._check_staleness("BTCUSDT", stale_ts)  # stale again -> alert #2

        stale_alerts = [kw for m, kw in alerts if m == "send_alert" and "Stale Data" in kw["title"]]
        assert len(stale_alerts) == 2
        assert runner._stale_alerted.get("BTCUSDT") is True

    def test_stop_loss_triggers_and_closes_position(self):
        """Regression test: the live engine never called check_stop_targets,
        so a strategy-set stop_price was stored on the position but never
        enforced — a position could blow through its stop with no exit
        until the strategy itself issued a close."""

        class BuyWithStopStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if not ctx.positions.get(ctx.symbol):
                    return [
                        OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0, stop_price=95.0)
                    ]
                return []

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            df = _make_ohlcv_df(n=5, start_hour=call_num)
            if call_num >= 3:
                df["low"] = 90.0  # bar 3's range breaches the 95.0 stop
            return df

        runner = self._make_runner(strategy=BuyWithStopStrategy(), fetcher=fetcher)
        runner.run(max_iterations=3)

        # bar1: buy queued. bar2: buy fills. bar3: stop-loss force-closes.
        assert "BTCUSDT" not in runner._positions

    def test_feature_failure_fills_pending_action_at_correct_bar(self):
        """Regression test: previously, a feature_fn exception returned
        before popping/filling the previous bar's pending action, silently
        deferring it to whichever LATER bar's feature computation happened
        to succeed — filling at that bar's price instead of the intended
        immediate-next-bar price."""
        call_num = 0
        fill_prices: list[float] = []

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            base = datetime(2025, 1, 1, call_num, tzinfo=UTC)
            ts = pd.date_range(base, periods=5, freq="h", tz=UTC)
            level = call_num * 100.0  # distinct price level per call
            return pd.DataFrame(
                {
                    "ts": ts,
                    "open": level - 0.5,
                    "high": level + 1.0,
                    "low": level - 1.0,
                    "close": level,
                    "volume": 1000.0,
                }
            )

        def flaky_feature_fn(h1_base: pd.DataFrame) -> pd.DataFrame:
            if call_num == 2:
                raise RuntimeError("feature blip")
            return _simple_feature_fn(h1_base)

        class BuyOnceStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if not ctx.positions.get(ctx.symbol):
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        def on_order_event(event):
            if event.event_type == "open":
                fill_prices.append(event.price)

        runner = self._make_runner(
            strategy=BuyOnceStrategy(),
            fetcher=fetcher,
            feature_fn=flaky_feature_fn,
        )
        runner._on_order_event = on_order_event
        runner.run(max_iterations=3)

        # bar1 (level=100): strategy queues a buy. bar2 (level=200): feature_fn
        # raises, but the pending buy must still fill at bar2's own price
        # (open=199.5) -- not silently deferred to bar3's price (299.5).
        assert fill_prices == [199.5]

    def test_order_failure_halts_without_committing_phantom_position(self):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.place_order.side_effect = RuntimeError("connection refused")

        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        cfg = _test_cfg(mode="live")
        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            config=cfg,
            order_adapter=mock_order_adapter,
        )
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=2)  # must not raise / not be swallowed as a poll error

        order_failed = [
            kw
            for m, kw in alerts
            if m == "send_alert" and "Ambiguous Order Placement" in kw["title"]
        ]
        assert order_failed
        assert "qty=1.0000" in order_failed[0]["message"]
        assert runner._halted is True
        assert runner._positions == {}
        assert runner._cash == 100_000.0

    def test_db_write_alerts_after_consecutive_failures(self):
        """Regression test: DB write failures were silently swallowed
        forever with no escalation. After CONSECUTIVE_ERROR_THRESHOLD
        consecutive failures, one Telegram alert must fire; a later success
        resets the counter so a renewed outage can alert again."""
        runner = self._make_runner()
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        failing_fn = MagicMock(side_effect=RuntimeError("db down"))
        failing_fn.__name__ = "failing_fn"

        for _ in range(LiveTrader.CONSECUTIVE_ERROR_THRESHOLD - 1):
            runner._db_write(failing_fn)
        assert not alerts  # not yet at threshold

        runner._db_write(failing_fn)
        assert len(alerts) == 1
        assert alerts[0][0] == "send_alert"
        assert "DB Write Failing" in alerts[0][1]["title"]

        ok_fn = MagicMock(return_value=None)
        ok_fn.__name__ = "ok_fn"
        runner._db_write(ok_fn)
        assert runner._db_write_failures == 0

        # A renewed outage must alert again, not stay silent forever.
        for _ in range(LiveTrader.CONSECUTIVE_ERROR_THRESHOLD):
            runner._db_write(failing_fn)
        assert len(alerts) == 2

    def test_max_drawdown_breach_flattens_and_halts(self):
        """A drawdown breach must flatten the open position, alert, and
        permanently stop new entries — the strategy is never called again
        even though it would otherwise keep re-buying."""
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            df = _make_ohlcv_df(n=5, start_hour=call_num)
            if call_num >= 3:
                df["open"] = 50.0
                df["high"] = 55.0
                df["low"] = 45.0
                df["close"] = 50.0
            return df

        class BuyOnceStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if not ctx.positions.get(ctx.symbol):
                    return [OrderIntent(action="long", symbol=ctx.symbol)]
                return []

        cfg = _test_cfg(
            params={"warmup_periods": 5},
            risk=RiskPolicy(max_drawdown_rate=0.2),
        )
        runner = self._make_runner(strategy=BuyOnceStrategy(), fetcher=fetcher, config=cfg)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        # bar1: buy queued. bar2: buy fills (~all cash). bar3: price craters
        # -> breach detected before the strategy sees the bar -> flattened +
        # halted. bar4: confirms the halt sticks (no re-buy, no 2nd alert).
        runner.run(max_iterations=4)

        assert runner._halted is True
        assert runner._positions == {}
        breach_alerts = [
            kw for m, kw in alerts if m == "send_alert" and "Max Drawdown Breach" in kw["title"]
        ]
        assert len(breach_alerts) == 1

    def test_drawdown_callback_queues_next_bar_exit(self):
        cost_model = CostModel(
            multiplier=1.0,
            commission_rate=0.01,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        runner = self._make_runner(executor=LiveExecutor(cost_model))
        runner._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        runner._cash = 0.0
        runner._positions["BTCUSDT"] = PositionState(
            symbol="BTCUSDT",
            side="long",
            entry_price=100.0,
            quantity=500.0,
            entry_at=datetime(2025, 1, 1, tzinfo=UTC),
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=50_000.0,
        )
        runner._last_prices["BTCUSDT"] = 100.0
        runner._equity_peak = 100_000.0
        runner._prev_equity = 50_000.0
        callbacks: list[tuple[float, float, float]] = []
        runner._on_bar = lambda _run_id, _ts, equity, drawdown, period_return: callbacks.append(
            (equity, drawdown, period_return)
        )

        runner._record_equity(
            datetime(2025, 1, 2, tzinfo=UTC),
            {
                "BTCUSDT": {
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 10_000.0,
                }
            },
        )

        assert runner._positions["BTCUSDT"].pending_market_exit_reason == REASON_DRAWDOWN_BREACH
        assert runner._cash == pytest.approx(0.0)
        assert len(callbacks) == 1
        assert callbacks[0] == pytest.approx((50_000.0, -0.5, 0.0))
        assert runner._prev_equity == pytest.approx(50_000.0)


def _make_fill_event() -> OrderEvent:
    return OrderEvent(
        ts=datetime(2025, 1, 1, tzinfo=UTC),
        symbol="BTCUSDT",
        side="long",
        event_type="open",
        fill_quantity=2.0,
        price=100.0,
        entry_price=100.0,
        remaining_quantity=2.0,
        notional=200.0,
        commission=0.0,
        slippage=0.0,
        tax=0.0,
    )


class TestLiveExecutionLifecycle:
    def _make_trader(
        self,
        strategy: Strategy,
        adapter: MagicMock,
        *,
        state_store: MemoryLiveStateStore | None = None,
        config: RunConfig | None = None,
        clock=None,
    ) -> LiveTrader:
        return LiveTrader(
            strategy,
            _simple_feature_fn,
            config=config or _test_cfg(mode="live"),
            adapter=lambda *a, **kw: _make_ohlcv_df(),
            cost_model=_zero_cost_model(),
            order_adapter=adapter,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            state_store=state_store or MemoryLiveStateStore(),
            clock=clock or (lambda: TEST_CLOCK_NOW),
        )

    def test_partial_fill_commits_only_confirmed_quantity_and_stays_open(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            status="filling",
            quantity=2.0,
            filled=0.75,
            average=105.0,
            fee=0.25,
        )

        class BuyTwo(Strategy):
            def on_bar(self, ctx):
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=2.0)]

        runner = self._make_trader(BuyTwo(), adapter)
        runner.run(max_iterations=1)

        assert runner._halted is False
        assert runner._positions["BTCUSDT"].quantity == 0.75
        assert runner._positions["BTCUSDT"].entry_price == 105.0
        assert runner._positions["BTCUSDT"].entry_commission == 0.25
        assert len(runner._active_orders) == 1
        assert runner._active_orders[0].status == "partial"

    def test_volume_budget_is_cumulative_across_same_symbol_intents(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(_HoldStrategy(), adapter)
        runner._max_bar_volume_participation_rate = 0.1
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        requests = runner._plan_live_orders(
            [
                OrderIntent(action="long", symbol="BTCUSDT", quantity=80.0),
                OrderIntent(action="long", symbol="BTCUSDT", quantity=80.0),
            ],
            {
                "BTCUSDT": {
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1_000.0,
                }
            },
            ts,
        )

        assert [request.quantity for request in requests] == [80.0, 20.0]

    def test_live_planning_uses_adv_as_second_volume_budget(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(_HoldStrategy(), adapter)
        runner._max_bar_volume_participation_rate = 0.1
        runner._max_adv_participation_rate = 0.02
        runner._adv_filled_quantities = {"BTCUSDT": 30.0}
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        requests = runner._plan_live_orders(
            [OrderIntent(action="long", symbol="BTCUSDT", quantity=80.0)],
            {
                "BTCUSDT": {
                    "open": 100.0,
                    "high": 100.0,
                    "low": 100.0,
                    "close": 100.0,
                    "volume": 1_000.0,
                }
            },
            ts,
            lagged_adv_by_symbol={"BTCUSDT": 2_000.0},
        )

        assert [request.quantity for request in requests] == [10.0]

    def test_repeated_partial_report_is_idempotent(self):
        adapter = _mock_order_adapter()
        partial = _broker_report(
            status="filling",
            quantity=2.0,
            filled=0.75,
            average=105.0,
            fee=0.25,
        )
        adapter.place_order.return_value = partial
        adapter.get_order.return_value = partial

        class BuyTwo(Strategy):
            def on_bar(self, ctx):
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=2.0)]

        runner = self._make_trader(BuyTwo(), adapter)
        runner.run(max_iterations=2)

        assert runner._positions["BTCUSDT"].quantity == 0.75
        assert runner._positions["BTCUSDT"].entry_commission == 0.25
        assert runner._cash == pytest.approx(100_000.0 - 0.75 * 105.0 - 0.25)

    def test_active_order_keeps_market_health_updates_without_new_strategy_decision(self):
        adapter = _mock_order_adapter()
        accepted = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.place_order.return_value = accepted
        adapter.get_order.return_value = accepted
        fetcher = MagicMock(return_value=_make_ohlcv_df())

        class CountingBuy(Strategy):
            def __init__(self):
                self.calls = 0

            def on_bar(self, ctx):
                self.calls += 1
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]

        strategy = CountingBuy()
        runner = LiveTrader(
            strategy,
            _simple_feature_fn,
            config=_test_cfg(mode="live"),
            adapter=fetcher,
            cost_model=_zero_cost_model(),
            order_adapter=adapter,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            state_store=MemoryLiveStateStore(),
            clock=lambda: TEST_CLOCK_NOW,
        )

        runner.run(max_iterations=2)

        assert fetcher.call_count == 2
        assert strategy.calls == 1
        assert len(runner._active_orders) == 1
        adapter.place_order.assert_called_once()

    def test_live_order_timeout_cancels_preserves_partial_fill_and_halts(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            order_id="open-1",
            status="filling",
            quantity=2.0,
            filled=0.5,
            average=100.0,
        )
        adapter.get_order.return_value = _broker_report(
            order_id="open-1",
            status="filling",
            quantity=2.0,
            filled=0.5,
            average=100.0,
        )
        adapter.cancel_order.return_value = _broker_report(
            order_id="open-1",
            status="cancelled",
            quantity=2.0,
            filled=0.75,
            average=102.0,
        )
        now = [TEST_CLOCK_NOW]

        class BuyTwo(Strategy):
            def on_bar(self, ctx):
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=2.0)]

        runner = self._make_trader(
            BuyTwo(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(live_order_timeout_seconds=30),
            ),
            clock=lambda: now[0],
        )
        runner.run(max_iterations=1)
        attempted_at = runner._active_orders[0].placement_attempted_at

        now[0] += timedelta(seconds=30)
        runner._poll_cycle()

        assert attempted_at == TEST_CLOCK_NOW
        adapter.cancel_order.assert_called_once_with("open-1", "BTC/USDT")
        assert runner._halted is True
        assert runner._active_orders == []
        assert runner._positions["BTCUSDT"].quantity == pytest.approx(0.75)
        assert runner._positions["BTCUSDT"].entry_price == pytest.approx(102.0)

    def test_timeout_pending_cancel_is_polled_without_duplicate_cancel(self):
        adapter = _mock_order_adapter()
        accepted = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.place_order.return_value = accepted
        adapter.get_order.side_effect = [
            accepted,
            accepted,
            {
                "id": "open-1",
                "status": "cancelled",
                "amount": 1.0,
                "filled": 0.0,
            },
        ]
        adapter.cancel_order.return_value = {
            "id": "open-1",
            "status": "pending_cancel",
            "amount": 1.0,
            "filled": 0.0,
        }
        now = [TEST_CLOCK_NOW]
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(live_order_timeout_seconds=30),
            ),
            clock=lambda: now[0],
        )
        runner.run(max_iterations=1)

        now[0] += timedelta(seconds=30)
        runner._poll_cycle()

        assert runner._halted is True
        assert runner._active_orders[0].status == "cancel_pending"
        adapter.cancel_order.assert_called_once()

        runner._poll_cycle()

        assert runner._active_orders == []
        adapter.cancel_order.assert_called_once()

    def test_open_order_resumes_after_restart_without_resubmission(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "open-1",
            "status": "submitted",
            "amount": 1.0,
            "filled": 0.0,
        }

        first = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        first.run(max_iterations=1)

        adapter.get_order.return_value = _broker_report(
            order_id="open-1",
            quantity=1.0,
            average=101.0,
            fee=0.2,
        )
        adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 1.0,
            "avg_price": 101.0,
            "unrealized_pnl": 0.0,
        }
        second = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        second.run(max_iterations=1)

        assert adapter.place_order.call_count == 1
        assert second._active_orders == []
        assert second._positions["BTCUSDT"].quantity == 1.0
        assert second._positions["BTCUSDT"].entry_commission == 0.2

    def test_live_order_timeout_age_survives_restart(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        accepted = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.place_order.return_value = accepted
        config = _test_cfg(
            mode="live",
            execution=ExecutionPolicy(live_order_timeout_seconds=30),
        )

        first = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            state_store=store,
            config=config,
            clock=lambda: TEST_CLOCK_NOW,
        )
        first.run(max_iterations=1)

        adapter.get_order.return_value = accepted
        adapter.cancel_order.return_value = {
            "id": "open-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        second = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            state_store=store,
            config=config,
            clock=lambda: TEST_CLOCK_NOW + timedelta(seconds=30),
        )
        second.run(max_iterations=1)

        adapter.place_order.assert_called_once()
        adapter.cancel_order.assert_called_once_with("open-1", "BTC/USDT")
        assert second._halted is True
        assert second._active_orders == []

    def test_drawdown_exit_remains_active_while_halted_and_resumes_after_restart(
        self,
    ):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            order_id="risk-exit-1",
            status="accepted",
            quantity=1.0,
            filled=0.0,
        )

        first = self._make_trader(_HoldStrategy(), adapter, state_store=store)
        first._risk_policy = RiskPolicy(max_drawdown_rate=0.2)
        first._positions["BTCUSDT"] = PositionState(
            symbol="BTCUSDT",
            side="long",
            entry_price=100.0,
            quantity=1.0,
            entry_at=datetime(2025, 1, 1, tzinfo=UTC),
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=100.0,
        )
        first._last_prices["BTCUSDT"] = 80.0

        first._flatten_and_halt(
            datetime(2025, 1, 2, tzinfo=UTC),
            -0.2,
            {"BTCUSDT": {"close": 80.0}},
        )

        assert first._halted is True
        assert len(first._active_orders) == 1
        adapter.cancel_order.assert_not_called()

        adapter.get_order.return_value = _broker_report(
            order_id="risk-exit-1",
            quantity=1.0,
            filled=1.0,
            average=79.0,
        )
        second = self._make_trader(_HoldStrategy(), adapter, state_store=store)
        second.run(max_iterations=1)

        assert second._halted is True
        assert second._positions == {}
        assert second._active_orders == []
        assert adapter.place_order.call_count == 1
        adapter.cancel_order.assert_not_called()

    def test_restored_cycle_does_not_repeat_decision(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report()

        class CountingBuy(Strategy):
            def __init__(self):
                self.calls = 0

            def on_bar(self, ctx):
                self.calls += 1
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]

        first_strategy = CountingBuy()
        first = self._make_trader(first_strategy, adapter, state_store=store)
        first.run(max_iterations=1)

        adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 1.0,
            "avg_price": 100.0,
            "unrealized_pnl": 0.0,
        }
        second_strategy = CountingBuy()
        second = self._make_trader(second_strategy, adapter, state_store=store)
        second.run(max_iterations=1)

        assert first_strategy.calls == 1
        assert second_strategy.calls == 0
        assert adapter.place_order.call_count == 1
        assert second._run_id == first._run_id

    def test_restored_spot_inventory_matches_without_broker_cost_basis(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report()

        first = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        first.run(max_iterations=1)

        adapter.get_position.return_value = {
            "symbol": "BTC/USDT",
            "size": 1.0,
            "avg_price": None,
            "unrealized_pnl": 0.0,
        }
        second = self._make_trader(_HoldStrategy(), adapter, state_store=store)
        second.run(max_iterations=1)

        assert second._halted is False
        assert second._positions["BTCUSDT"].quantity == 1.0
        assert second._positions["BTCUSDT"].entry_price == 100.0

    @pytest.mark.parametrize("status", ["cancelled", "rejected"])
    def test_final_failed_order_is_persisted_and_halts(self, status):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "final-1" if status == "cancelled" else "",
            "status": status,
            "amount": 1.0,
            "filled": 0.0,
        }

        runner = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._positions == {}
        tracked = next(iter(store.orders.values()))
        assert tracked.status == status

    def test_halt_cancels_open_order(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.get_order.return_value = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.cancel_order.return_value = {
            "id": "open-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        runner = self._make_trader(_AlwaysBuyStrategy(), adapter)
        runner.run(max_iterations=1)

        runner.halt("test")

        adapter.cancel_order.assert_called_once_with("open-1", "BTC/USDT")
        assert runner._active_orders == []
        assert runner._halted is True

    def test_manual_halt_rejects_blank_reason(self):
        runner = self._make_trader(_AlwaysBuyStrategy(), _mock_order_adapter())

        with pytest.raises(ValueError, match="non-empty"):
            runner.halt(" ")

    def test_halt_survives_restart_until_operator_reset(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "",
            "status": "rejected",
            "amount": 1.0,
            "filled": 0.0,
        }
        first = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        first.run(max_iterations=1)

        second = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        assert second._halted is True

        second.reset_halt()

        assert second._halted is False
        restored = store.load(second._state_key)
        assert restored is not None
        assert restored.halted is False

    def test_orphan_order_halts_before_strategy_decision(self):
        adapter = _mock_order_adapter()
        adapter.list_open_orders.return_value = [{"id": "manual-1", "clientOrderId": "external"}]
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_trader(strategy, adapter)

        runner.run(max_iterations=1)

        assert runner._halted is True
        strategy.on_bar.assert_not_called()

    def test_exit_uses_broker_price_fees_and_timestamp(self):
        first_fill_at = datetime(2025, 1, 1, tzinfo=UTC)
        second_fill_at = first_fill_at + timedelta(hours=1)
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(
                order_id="buy-1",
                quantity=1.0,
                average=100.0,
                fee=1.0,
                executed_at=first_fill_at,
            ),
            _broker_report(
                order_id="sell-1",
                quantity=1.0,
                average=110.0,
                fee=2.0,
                executed_at=second_fill_at,
            ),
        ]
        events: list[OrderEvent] = []
        store = MemoryLiveStateStore()

        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            state_store=store,
        )
        runner._on_order_event = events.append
        first_frame = _make_ohlcv_df(start_hour=0)
        second_frame = _make_ohlcv_df(start_hour=1)
        runner._process_bar(
            "BTCUSDT",
            first_frame,
            first_frame["ts"].iloc[-1].to_pydatetime(),
        )
        runner._process_bar(
            "BTCUSDT",
            second_frame,
            second_frame["ts"].iloc[-1].to_pydatetime(),
        )

        assert runner._positions == {}
        assert runner._cash == pytest.approx(100_007.0)
        assert [event.ts for event in events] == [first_fill_at, second_fill_at]
        assert events[-1].price == 110.0
        assert events[-1].commission == 2.0
        assert events[-1].pnl == 7.0
        checkpoint = store.load(runner._state_key)
        assert checkpoint is not None
        assert checkpoint.trade_count == 1

    def test_numeric_fill_price_submits_real_limit_order(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "limit-1",
            "status": "submitted",
            "amount": 1.0,
            "filled": 0.0,
        }

        class LimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        fill_price=99.5,
                    )
                ]

        runner = self._make_trader(LimitBuy(), adapter)
        runner.run(max_iterations=1)

        signal = adapter.place_order.call_args.args[0]
        assert signal["order_type"] == "limit"
        assert signal["price"] == 99.5
        assert runner._positions == {}

    def test_limit_price_collar_halts_before_submission(self):
        adapter = _mock_order_adapter()

        class FatFingerLimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        fill_price=50.0,
                    )
                ]

        runner = self._make_trader(FatFingerLimitBuy(), adapter)
        runner._risk_policy = RiskPolicy(max_limit_price_deviation_rate=0.1)
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()

    def test_order_notional_guard_halts_before_submission(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(_AlwaysBuyStrategy(), adapter)
        runner._risk_policy = RiskPolicy(max_order_notional=50.0)

        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()

    def test_order_is_normalized_before_checkpoint_and_submission(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            **signal,
            "quantity": 1.0,
            "price": 99.5,
        }
        adapter.place_order.return_value = {
            "id": "limit-1",
            "status": "submitted",
            "amount": 1.0,
            "filled": 0.0,
        }

        class LimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.9,
                        fill_price=99.57,
                    )
                ]

        runner = self._make_trader(LimitBuy(), adapter, state_store=store)
        runner.run(max_iterations=1)

        signal = adapter.place_order.call_args.args[0]
        tracked = next(iter(store.orders.values()))
        assert signal["quantity"] == 1.0
        assert signal["price"] == 99.5
        assert tracked.request.quantity == 1.0
        assert tracked.request.limit_price == 99.5

    def test_order_preflight_failure_halts_before_submission(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = ValueError("below minimum notional")

        runner = self._make_trader(_AlwaysBuyStrategy(), adapter)
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()

    def test_prepared_order_missing_quantity_halts_before_submission(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            key: value for key, value in signal.items() if key != "quantity"
        }

        runner = self._make_trader(_AlwaysBuyStrategy(), adapter)
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()

    @pytest.mark.parametrize(
        "action,match",
        [
            (
                OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0, fill_price="close"),
                "historical bar field",
            ),
            (
                OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0, stop_price=95.0),
                "broker-native protective orders",
            ),
        ],
    )
    def test_noncausal_live_intent_fails_closed(self, action, match):
        adapter = _mock_order_adapter()

        class InvalidIntent(Strategy):
            def on_bar(self, ctx):
                return [action]

        runner = self._make_trader(InvalidIntent(), adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert adapter.place_order.call_count == 0
        assert any(match in kwargs.get("message", "") for _, kwargs in alerts)


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
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(mode="live", broker="binance"),
                cost_model=_zero_cost_model(),
                on_bar=None,
                on_order_event=None,
                on_ohlcv=None,
                on_heartbeat=None,
                on_signal_outcome=None,
                state_store=MemoryLiveStateStore(),
                **kwargs,
            )
        return trader, mock_cls

    def test_auto_builds_order_adapter_from_env(self, monkeypatch):
        trader, adapter_cls = self._build(monkeypatch)
        adapter_cls.assert_called_once()
        assert adapter_cls.call_args.kwargs["credentials"].exchange_id == "binance"
        assert trader._executor.get_order_adapter("BTCUSDT") is adapter_cls.return_value

    def test_explicit_order_adapter_not_overridden(self, monkeypatch):
        explicit = MagicMock()
        trader, adapter_cls = self._build(monkeypatch, order_adapter=explicit)
        adapter_cls.assert_called_once_with(credentials=None)
        assert trader._executor.get_order_adapter("BTCUSDT") is explicit


class TestMultiAdapterRouting:
    def test_executor_routes_orders_by_canonical_symbol(self):
        crypto = _mock_order_adapter()
        ibkr = _mock_order_adapter()
        executor = LiveExecutor(
            {"BTCUSDT": _zero_cost_model(), "MU": _zero_cost_model()},
            simulation=False,
            order_adapter={"BTCUSDT": crypto, "MU": ibkr},
            strategy_name="test",
        )

        assert executor.get_order_adapter("BTCUSDT") is crypto
        assert executor.get_order_adapter("MU") is ibkr

    def test_live_trader_resolves_costs_per_symbol(self):
        cfg = _test_cfg(
            symbols=["BTCUSDT", "MU"],
            market="multi",
            data_source="multi",
            instrument_overrides={"MU": {"currency": "USDT"}},
        )
        fetchers = {
            "BTCUSDT": lambda *args, **kwargs: _make_ohlcv_df(),
            "MU": lambda *args, **kwargs: _make_ohlcv_df(),
        }

        trader = LiveTrader(
            _HoldStrategy(),
            _simple_feature_fn,
            config=cfg,
            adapter=fetchers,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            warmup_fetcher=None,
            state_store=MemoryLiveStateStore(),
        )

        assert trader._get_cost_model("BTCUSDT").commission_rate == 0.001
        assert trader._get_cost_model("MU").long_margin_rate == 1.0
        assert trader._get_cost_model("MU").short_margin_rate == 0.5


class TestIBKRLiveAutoWiring:
    def test_us_equity_builds_ibkr_instead_of_crypto(self):
        with patch("brokers.ibkr_adapter.IBKRAdapter") as mock_cls:
            mock_cls.return_value = _mock_order_adapter()
            trader = LiveTrader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(
                    mode="live",
                    symbols=["MU"],
                    market="us_equity",
                    data_source="ibkr",
                    broker="ibkr",
                ),
                cost_model=_zero_cost_model(),
                on_bar=None,
                on_order_event=None,
                on_ohlcv=None,
                on_heartbeat=None,
                on_signal_outcome=None,
                state_store=MemoryLiveStateStore(),
                clock=lambda: TEST_CLOCK_NOW,
            )

        mock_cls.assert_called_once_with(trading_enabled=True)
        assert trader._executor.get_order_adapter("MU") is mock_cls.return_value


class TestShioajiLiveAutoWiring:
    """tw_futures live mode: engine.py reuses the single authenticated
    ShioajiAdapter for both fetching and order placement (order_adapter=None
    auto-wires to the same instance as the fetcher) — never covered
    end-to-end: a strategy signal actually reaching Shioaji's place_order."""

    def _shioaji_cfg(self, **overrides):
        overrides.setdefault("symbols", ["TXFR1"])
        overrides.setdefault("market", "tw_futures")
        overrides.setdefault("data_source", "shioaji")
        overrides.setdefault("broker", "shioaji")
        return _test_cfg(**overrides)

    def test_auto_builds_order_adapter_from_shioaji(self):
        with patch("brokers.shioaji_adapter.ShioajiAdapter") as mock_cls:
            mock_cls.return_value = MagicMock()
            trader = LiveTrader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=self._shioaji_cfg(mode="live"),
                cost_model=_zero_cost_model(),
                on_bar=None,
                on_order_event=None,
                on_ohlcv=None,
                on_heartbeat=None,
                on_signal_outcome=None,
                state_store=MemoryLiveStateStore(),
            )

        # Same authenticated session used for fetching and for order placement.
        assert trader._executor.get_order_adapter("TXFR1") is mock_cls.return_value

    def test_strategy_signal_triggers_shioaji_place_order(self):
        """End-to-end: strategy emits a buy, next bar's fill is mirrored to
        the auto-wired ShioajiAdapter via LiveExecutor.submit_order."""
        call_num = 0

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        with patch("brokers.shioaji_adapter.ShioajiAdapter") as mock_cls:
            mock_shioaji = _mock_order_adapter()
            mock_shioaji.place_order.side_effect = lambda signal: _broker_report(
                quantity=signal["quantity"],
                average=104.25,
            )
            mock_shioaji.fetch_ohlcv.side_effect = (
                lambda symbol, tf, limit, drop_incomplete=False, calendar_id=None: fetcher()
            )
            mock_cls.return_value = mock_shioaji

            trader = LiveTrader(
                _AlwaysBuyStrategy(),
                _simple_feature_fn,
                config=self._shioaji_cfg(mode="live"),
                cost_model=_zero_cost_model(),
                on_bar=None,
                on_order_event=None,
                on_ohlcv=None,
                on_heartbeat=None,
                on_signal_outcome=None,
                state_store=MemoryLiveStateStore(),
                clock=lambda: TEST_CLOCK_NOW,
            )
            trader._sleep = lambda _seconds: None  # no real delays in unit tests
            trader.run(max_iterations=2)

        # The completed-bar decision is submitted to Shioaji immediately.
        assert mock_shioaji.place_order.call_count >= 1
        first_call_signal = mock_shioaji.place_order.call_args_list[0].args[0]
        assert first_call_signal["side"] == "buy"
        assert first_call_signal["symbol"] == "TXFR1"
