"""Unit tests for LiveTrader and LiveExecutor.

All tests use mocks — no real API calls, no DB, no Telegram.

Skills: python, quant
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from threading import Event, get_ident
from time import perf_counter
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from librae.core.cost_model import CostModel
from librae.core.executor import (
    REASON_DRAWDOWN_BREACH,
    PositionEvent,
    execute_order_intents,
    validate_strategy_decision,
)
from librae.core.run_config import AccountConfig, ExecutionPolicy, RiskPolicy, RunConfig
from librae.core.strategy import (
    Context,
    OrderIntent,
    PortfolioWeights,
    PositionState,
    Strategy,
)
from librae.live.engine import LiveTrader
from librae.live.executor import ExecutionReport, LiveExecutor, OrderRequest, PositionRequest
from librae.live.state import MemoryLiveStateStore, TrackedOrder
from librae.orchestration.live import build_live_trader

from tests.conftest import make_test_cfg

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _zero_cost_model() -> CostModel:
    return CostModel.zero()


def test_sim_engine_does_not_build_optional_infrastructure():
    config = make_test_cfg(mode="sim")

    trader = LiveTrader(
        MagicMock(),
        lambda frame: frame,
        config=config,
        adapter=MagicMock(),
        cost_model=CostModel.zero(),
    )

    assert trader._state_store is None
    assert trader._on_bar is None


def _mock_order_adapter(mock_class: type[MagicMock] = MagicMock) -> MagicMock:
    """order_adapter mock with realistic flat get_position()/get_balance() —
    a bare MagicMock's auto-generated return values are truthy/float-coercible
    by default, which _reconcile_positions()/_reconcile_cash() would misread
    as real broker state (an open position, a MagicMock "total") at startup."""
    adapter = mock_class()
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
    ts = pd.date_range(end=pd.Timestamp(ts_end).floor("h"), periods=n, freq="h", tz=UTC)
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


TEST_CLOCK_NOW = datetime(2025, 1, 1, 10, tzinfo=UTC)


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
    warmup_periods = overrides.pop("warmup_periods", 5)
    overrides.setdefault(
        "execution",
        ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=warmup_periods,
        ),
    )
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
            if route.get("data_adapter") != "ibkr":
                route.setdefault("calendar_id", "24/7")
    if "account" not in overrides:
        currencies = {
            routes.get(symbol, {}).get("currency") or registry[symbol].currency
            for symbol in symbols
        }
        if len(currencies) != 1:
            raise ValueError("test config symbols must share one account currency")
        overrides["account"] = AccountConfig(
            currency=next(iter(currencies)),
            initial_cash=100_000.0,
        )
    if routes:
        overrides["instrument_overrides"] = routes
    return make_test_cfg(**overrides)


# ---------------------------------------------------------------------------
# LiveExecutor tests
# ---------------------------------------------------------------------------


class TestLiveExecutor:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"venue_symbol": ""}, "venue_symbol"),
            ({"currency": ""}, "currency"),
            ({"multiplier": 0.0}, "multiplier"),
            ({"multiplier": True}, "multiplier"),
        ],
    )
    def test_position_request_rejects_missing_accounting_identity(self, kwargs, message):
        values = {
            "symbol": "BTCUSDT",
            "venue_symbol": "BTC/USDT",
            "currency": "USDT",
            "multiplier": 1.0,
        }
        values.update(kwargs)

        with pytest.raises(ValueError, match=message):
            PositionRequest(**values)

    def test_future_position_request_requires_explicit_contract_selection(self):
        with pytest.raises(ValueError, match="FUT position requires"):
            PositionRequest(
                symbol="ES",
                venue_symbol="ES",
                currency="USD",
                multiplier=50.0,
                security_type="FUT",
                exchange="CME",
            )

    def test_future_order_request_rejects_conflicting_contract_selection(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            OrderRequest(
                client_order_id="test-1",
                symbol="ES_202609",
                side="buy",
                quantity=1.0,
                order_type="market",
                submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
                security_type="FUT",
                continuous_alias=True,
                contract_month="202609",
            )

    def test_live_adapter_requires_position_reconciliation_capability(self):
        class LifecycleOnly:
            def prepare_order(self, signal):
                return signal

            def place_order(self, signal):
                return {}

            def find_order(self, client_order_id, symbol):
                return None

            def get_order(self, order_id, symbol):
                return {}

            def list_open_orders(self, symbol):
                return []

            def cancel_order(self, order_id, symbol):
                return {}

        with pytest.raises(ValueError, match="missing required methods: get_position"):
            LiveExecutor(
                _zero_cost_model(),
                simulation=False,
                order_adapter=LifecycleOnly(),
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
        event = PositionEvent(
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

    @pytest.mark.parametrize("event_type", ["open", "add", "reduce", "close"])
    @pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
    def test_client_order_id_stays_within_broker_length_limit(self, symbol, event_type):
        """The readable id alone can already exceed Binance's 36-char limit
        for ordinary symbols, regardless of strategy_name (issue #89)."""
        ex = LiveExecutor(
            _zero_cost_model(),
            simulation=False,
            strategy_name="a_reasonably_long_strategy_name",
            order_adapter=MagicMock(),
        )
        event = PositionEvent(
            ts=datetime(2025, 1, 1, tzinfo=UTC),
            symbol=symbol,
            side="long",
            event_type=event_type,
            fill_quantity=1.0,
            price=100.0,
            entry_price=100.0,
            remaining_quantity=1.0,
            notional=100.0,
            commission=0.0,
            slippage=0.0,
            tax=0.0,
        )
        request = ex.request_from_event(event, sequence=3)
        assert 1 <= len(request.client_order_id) <= 36
        assert re.fullmatch(r"[a-zA-Z0-9_-]+", request.client_order_id)

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

    @pytest.mark.parametrize("route", ["placement", "polling", "restart"])
    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("symbol", "ETH/USDT", "symbol"),
            ("side", "sell", "side"),
            ("clientOrderId", "wrong-client", "client order id"),
            ("amount", 0.5, "requested quantity"),
        ],
    )
    def test_order_routes_reject_mismatched_broker_identity(
        self, route, field, value, message, caplog
    ):
        adapter = MagicMock()
        raw = {
            "id": "broker-1",
            "clientOrderId": "client-1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "status": "submitted",
            "amount": 1.0,
            field: value,
        }
        request = OrderRequest(
            client_order_id="client-1",
            symbol="BTCUSDT",
            venue_symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        ex = LiveExecutor(_zero_cost_model(), simulation=False, order_adapter=adapter)

        if route == "placement":
            adapter.place_order.return_value = raw
            with caplog.at_level(logging.ERROR, logger="librae.live.executor"):
                assert ex.submit_order(request) is None
            assert message in caplog.text
        elif route == "polling":
            adapter.get_order.return_value = raw
            with pytest.raises(ValueError, match=message):
                ex.get_order(request, "broker-1")
        else:
            adapter.find_order.return_value = raw
            with pytest.raises(ValueError, match=message):
                ex.find_order(request)

    def test_normalized_report_retains_canonical_identity_after_raw_validation(self):
        request = OrderRequest(
            client_order_id="client-1",
            symbol="BTCUSDT",
            venue_symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        report = LiveExecutor.normalize_report(
            request,
            {
                "id": "broker-1",
                "client_order_id": "client-1",
                "symbol": "BTC/USDT",
                "side": "BUY",
                "status": "submitted",
                "requested_quantity": 1.0,
            },
        )

        assert report.client_order_id == "client-1"
        assert report.symbol == "BTCUSDT"
        assert report.side == "buy"
        assert report.requested_quantity == 1.0

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
        if test_config.mode == "live":
            kwargs.setdefault("runtime_revision", "test-runtime")
        runner = LiveTrader(
            strategy or _HoldStrategy(),
            feature_fn or _simple_feature_fn,
            config=test_config,
            adapter=fetcher or (lambda *a, **kw: _make_ohlcv_df()),
            cost_model=(
                executor.get_cost_model(test_config.symbol) if executor else _zero_cost_model()
            ),
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            warmup_fetcher=None,
            **kwargs,
        )
        runner._sleep = lambda _seconds: None  # no real delays in unit tests
        # Most fixtures use a fixed clock to exercise execution rather than
        # staleness. Dedicated staleness tests restore the production bound.
        runner.STALE_DATA_TOLERANCE_BARS = 100
        return runner

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("client_order_id", "wrong-client", "identity"),
            ("symbol", "ETHUSDT", "identity"),
            ("side", "sell", "identity"),
            ("requested_quantity", 0.5, "requested quantity"),
        ],
    )
    def test_report_validation_precedes_tracked_or_portfolio_mutation(self, field, value, message):
        request = OrderRequest(
            client_order_id="client-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=TEST_CLOCK_NOW,
        )
        tracked = TrackedOrder(request=request, placement_attempted=True)
        values = {
            "order_id": "broker-1",
            "client_order_id": request.client_order_id,
            "symbol": request.symbol,
            "side": request.side,
            "status": "submitted",
            "requested_quantity": request.quantity,
            "filled_quantity": 0.0,
            "average_price": None,
            "commission": 0.0,
            "slippage": 0.0,
            "tax": 0.0,
            "executed_at": None,
            field: value,
        }
        before = tracked.to_dict()

        with pytest.raises(ValueError, match=message):
            LiveTrader._apply_order_report(
                LiveTrader.__new__(LiveTrader), tracked, ExecutionReport(**values)
            )

        assert tracked.to_dict() == before

    def test_max_iterations_stops(self):
        runner = self._make_runner()
        runner.run(max_iterations=2)
        # Should not hang — reaching here means it stopped

    def test_market_data_fetch_concurrency_is_explicit_and_bounded(self):
        started = {"AAA": Event(), "BBB": Event()}

        def fetcher(symbol, *_args, **_kwargs):
            started[symbol].set()
            other = "BBB" if symbol == "AAA" else "AAA"
            assert started[other].wait(timeout=1)
            return _make_ohlcv_df()

        runner = self._make_runner(
            fetcher=fetcher,
            config=_test_cfg(
                symbols=["AAA", "BBB"],
                market_data_workers=2,
            ),
        )

        frames = runner._fetch_runtime_frames()

        assert list(frames) == ["AAA", "BBB"]
        assert set(runner._cycle_fetch_seconds) == {"AAA", "BBB"}

    def test_concrete_market_data_adapter_uses_resolved_route(self):
        calls: list[tuple[tuple, dict]] = []

        class Adapter:
            def fetch_ohlcv(self, *args, **kwargs):
                calls.append((args, kwargs))
                return _make_ohlcv_df()

        runner = self._make_runner(fetcher=Adapter())

        frame = runner._fetch_with_cache("BTCUSDT")

        assert frame is not None
        assert calls == [
            (
                ("BTC/USDT", "1h"),
                {
                    "limit": 6,
                    "continuous_alias": False,
                    "contract_month": None,
                    "drop_incomplete": True,
                },
            )
        ]

    def test_ibkr_adapter_receives_generic_session_and_calendar_contract(self):
        calls: list[tuple[tuple, dict]] = []
        frame = _make_ohlcv_df()
        frame["ts"] = pd.date_range(
            "2025-01-02T14:30:00Z",
            periods=5,
            freq="h",
        )

        class Adapter:
            market_data_route = "ibkr"

            def fetch_ohlcv(self, *args, **kwargs):
                calls.append((args, kwargs))
                return frame

        config = _test_cfg(
            symbols=["AAPL"],
            market="us_equity",
            data_source="ibkr",
            session_mode="regular",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                    "calendar_id": "XNYS",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )
        runner = self._make_runner(
            fetcher=Adapter(),
            config=config,
            clock=lambda: datetime(2025, 1, 3, tzinfo=UTC),
        )

        frame = runner._fetch_with_cache("AAPL")

        assert frame is not None
        assert calls[0][1]["session_mode"] == "regular"
        assert calls[0][1]["calendar_id"] == "XNYS"
        assert "use_rth" not in calls[0][1]

    def test_daily_ibkr_without_calendar_fails_at_construction(self):
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )

        adapter = MagicMock()
        adapter.market_data_route = "ibkr"

        with pytest.raises(ValueError, match=r"daily IBKR.*calendar_id.*AAPL"):
            self._make_runner(fetcher=adapter, config=config)

    def test_daily_caller_owned_fetcher_supplies_calendar_outside_instrument_route(self):
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )

        def fetcher(*_args, **_kwargs):
            return _make_ohlcv_df()

        fetcher.market_data_calendar_id = "XNYS"

        runner = self._make_runner(fetcher=fetcher, config=config)

        assert runner._fetchers["AAPL"] is fetcher
        assert runner._market_data_subscriptions["AAPL"].calendar_id == "XNYS"

    def test_repeated_fetch_failure_suppresses_heartbeat_and_alerts_once(self):
        state = {"fail": True}

        def fetcher(*_args, **_kwargs):
            if state["fail"]:
                raise RuntimeError("feed unavailable")
            return _make_ohlcv_df()

        runner = self._make_runner(
            fetcher=fetcher,
            config=_test_cfg(warmup_periods=1),
        )
        heartbeat = MagicMock()
        runtime_events = []
        runner._on_heartbeat = heartbeat
        runner._on_runtime_event = runtime_events.append
        runner._notify = MagicMock()

        for _ in range(4):
            runner._poll_cycle()

        heartbeat.assert_not_called()
        failures = [
            event
            for event in runtime_events
            if event.detail.get("reason") == "market_data_fetch_failed"
        ]
        assert len(failures) == 1
        assert failures[0].detail["consecutive_failures"] == 3
        alerts = [
            call
            for call in runner._notify.call_args_list
            if call.args == ("send_alert",) and "Market Data Fetch Failed" in call.kwargs["title"]
        ]
        assert len(alerts) == 1

        state["fail"] = False
        runner._poll_cycle()

        heartbeat.assert_called_once_with(runner.run_id)

        state["fail"] = True
        for _ in range(3):
            runner._poll_cycle()

        failures = [
            event
            for event in runtime_events
            if event.detail.get("reason") == "market_data_fetch_failed"
        ]
        assert len(failures) == 2
        alerts = [
            call
            for call in runner._notify.call_args_list
            if call.args == ("send_alert",) and "Market Data Fetch Failed" in call.kwargs["title"]
        ]
        assert len(alerts) == 2

    def test_factory_rejects_daily_ibkr_before_building_adapter(self):
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )

        with (
            patch("librae.orchestration.live._build_adapter") as build_adapter,
            pytest.raises(ValueError, match=r"daily IBKR.*calendar_id.*AAPL"),
        ):
            build_live_trader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=config,
                database_enabled=False,
            )

        build_adapter.assert_not_called()

    def test_factory_daily_override_supplies_calendar_outside_instrument_route(self):
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )

        def fetcher(*_args, **_kwargs):
            return _make_ohlcv_df()

        fetcher.market_data_calendar_id = "XNYS"

        with patch("librae.orchestration.live._build_adapter") as build_adapter:
            runner = build_live_trader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=config,
                database_enabled=False,
                data_adapter_overrides={"AAPL": fetcher},
            )

        build_adapter.assert_not_called()
        assert runner._fetchers["AAPL"] is fetcher

    def test_daily_caller_owned_fetcher_without_any_calendar_identity_fails_closed(self):
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )

        with pytest.raises(ValueError, match=r"must declare market_data_calendar_id"):
            self._make_runner(fetcher=lambda *_args, **_kwargs: _make_ohlcv_df(), config=config)

    def test_caller_owned_calendar_cannot_conflict_with_instrument_identity(self):
        def fetcher(*_args, **_kwargs):
            return _make_ohlcv_df()

        fetcher.market_data_calendar_id = "XNYS"

        with pytest.raises(ValueError, match=r"source calendar_id='XNYS'.*configured.*'24/7'"):
            self._make_runner(fetcher=fetcher)

    def test_non_ibkr_concrete_adapter_rejects_regular_session_request(self):
        class Adapter:
            def fetch_ohlcv(self, *_args, **_kwargs):
                return _make_ohlcv_df()

        with pytest.raises(ValueError, match="cannot honor session_mode='regular'"):
            self._make_runner(
                fetcher=Adapter(),
                config=_test_cfg(session_mode="regular"),
            )

    def test_invalid_market_data_adapter_fails_at_construction(self):
        with pytest.raises(TypeError, match=r"bar-data callable.*fetch_ohlcv"):
            self._make_runner(fetcher=object())

    def test_enriched_market_data_columns_reach_feature_and_strategy(self):
        frame = _make_ohlcv_df()
        frame["factor_score"] = 0.75
        observed: list[float] = []

        class CaptureFactor(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                observed.append(float(ctx.bar["factor_score"]))
                return []

        runner = self._make_runner(
            strategy=CaptureFactor(),
            fetcher=lambda *_args, **_kwargs: frame,
        )

        runner.run(max_iterations=1)

        assert observed == [0.75]

    def test_market_data_is_not_featured_before_available_at(self):
        frame = _make_ohlcv_df(n=2)
        frame["close"] = [100.0, 999.0]
        frame["available_at"] = pd.to_datetime(["2025-01-01T01:00:00Z", "2025-01-01T03:00:00Z"])
        observed: list[float] = []

        class CaptureClose(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                observed.append(float(ctx.bar["close"]))
                return []

        runner = self._make_runner(
            strategy=CaptureClose(),
            fetcher=lambda *_args, **_kwargs: frame,
            config=_test_cfg(warmup_periods=1),
            clock=lambda: datetime(2025, 1, 1, 2, tzinfo=UTC),
        )

        runner.run(max_iterations=1)

        assert observed == [100.0]
        assert runner._ohlcv_cache["BTCUSDT"]["close"].tolist() == [100.0]

    def test_market_data_derives_completion_and_excludes_open_h1_bar(self):
        frame = _make_ohlcv_df(n=2, start_hour=1)
        frame["close"] = [100.0, 999.0]
        observed: list[float] = []

        class CaptureClose(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                observed.append(float(ctx.bar["close"]))
                return []

        runner = self._make_runner(
            strategy=CaptureClose(),
            fetcher=lambda *_args, **_kwargs: frame,
            config=_test_cfg(warmup_periods=1),
            clock=lambda: datetime(2025, 1, 1, 2, 30, tzinfo=UTC),
        )

        runner.run(max_iterations=1)

        assert observed == [100.0]
        assert runner._ohlcv_cache["BTCUSDT"]["ts"].tolist() == [
            pd.Timestamp("2025-01-01T01:00:00Z")
        ]

    def test_market_data_newest_first_is_stably_normalized_before_eligibility(self):
        frame = _make_ohlcv_df(n=2).iloc[::-1]
        runner = self._make_runner(
            fetcher=lambda *_args, **_kwargs: frame,
            config=_test_cfg(warmup_periods=1),
            clock=lambda: datetime(2025, 1, 1, 2, tzinfo=UTC),
        )

        result = runner._eligible_runtime_rows("BTCUSDT", frame)

        assert result["ts"].tolist() == [
            pd.Timestamp("2025-01-01T00:00:00Z"),
            pd.Timestamp("2025-01-01T01:00:00Z"),
        ]

    @pytest.mark.parametrize("mode", ["sim", "live"])
    def test_availability_metadata_is_audit_only_across_runtime_views(self, mode):
        from librae.core.executor import execute_pending_decision_and_stops

        frame = _make_ohlcv_df(n=1)
        expected_availability = pd.Timestamp("2025-01-01T01:00:00Z")
        frame["available_at"] = [expected_availability]
        feature_columns: list[set[str]] = []
        contexts: list[Context] = []
        audit_bars: list[dict[str, object]] = []

        def feature(history: pd.DataFrame) -> pd.DataFrame:
            feature_columns.append(set(history.columns))
            return _simple_feature_fn(history)

        class CaptureContext(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                contexts.append(ctx)
                return []

        runner = self._make_runner(
            strategy=CaptureContext(),
            fetcher=lambda *_args, **_kwargs: frame,
            feature_fn=feature,
            config=_test_cfg(mode=mode, warmup_periods=1),
            order_adapter=_mock_order_adapter() if mode == "live" else None,
            clock=lambda: datetime(2025, 1, 1, 2, tzinfo=UTC),
        )
        runner._on_ohlcv = lambda _symbol, _timeframe, bar, _ts: audit_bars.append(bar)
        eligible = runner._eligible_runtime_rows("BTCUSDT", frame)

        if mode == "sim":
            with patch(
                "librae.live.engine.execute_pending_decision_and_stops",
                wraps=execute_pending_decision_and_stops,
            ) as execute:
                runner._process_cycle(
                    {"BTCUSDT": eligible},
                    datetime(2025, 1, 1, tzinfo=UTC),
                )
            execution_bars = execute.call_args.args[4]
        else:
            runner._pending_decision = [OrderIntent(action="long", symbol="BTCUSDT", quantity=1.0)]
            with patch.object(runner, "_execute_live_decision", return_value=True) as execute:
                runner._process_cycle(
                    {"BTCUSDT": eligible},
                    datetime(2025, 1, 1, tzinfo=UTC),
                )
            execution_bars = execute.call_args.args[1]

        assert feature_columns == [{"open", "high", "low", "close", "volume"}]
        assert contexts
        assert "available_at" not in contexts[0].bar
        assert all("available_at" not in bar for bar in contexts[0].bars.values())
        assert all("available_at" not in bar for bar in execution_bars.values())
        assert audit_bars[0]["available_at"] == expected_availability

    def test_extended_daily_provider_availability_reaches_ohlcv_audit_callback(self):
        availability = pd.Timestamp("2026-03-10T13:30:00Z")
        frame = _make_ohlcv_at([datetime(2026, 3, 9, 13, 30, tzinfo=UTC)])
        frame["available_at"] = [availability]
        config = _test_cfg(
            symbols=["AAPL"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=1,
            ),
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "AAPL": {
                    "data_adapter": "ibkr",
                    "instrument_type": "spot",
                    "currency": "USD",
                    "security_type": "STK",
                    "exchange": "SMART",
                    "calendar_id": "XNYS",
                }
            },
            symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
        )
        runner = self._make_runner(
            fetcher=lambda *_args, **_kwargs: frame,
            config=config,
            clock=lambda: datetime(2026, 3, 11, tzinfo=UTC),
        )
        audit_bars: list[dict[str, object]] = []
        runner._on_ohlcv = lambda _symbol, _timeframe, bar, _ts: audit_bars.append(bar)

        eligible = runner._eligible_runtime_rows("AAPL", frame)
        runner._process_cycle(
            {"AAPL": eligible},
            datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        )

        assert audit_bars[0]["available_at"] == availability

    @pytest.mark.parametrize(
        ("timeframe", "calendar_id", "cached_ts", "fetched_ts", "message"),
        [
            (
                "D2",
                "24/7",
                "2026-01-01T00:00:00Z",
                "2026-01-02T00:00:00Z",
                "not aligned to timeframe=D2",
            ),
            (
                "H1",
                "XNYS",
                "2026-03-09T22:15:00Z",
                "2026-03-09T22:45:00Z",
                "timestamps overlap timeframe=H1",
            ),
        ],
    )
    def test_cross_batch_overlapping_bars_fail_before_cache_store(
        self,
        timeframe: str,
        calendar_id: str,
        cached_ts: str,
        fetched_ts: str,
        message: str,
    ):
        symbol = "AAA"
        runner = self._make_runner(
            config=_test_cfg(
                symbols=[symbol],
                timeframe=timeframe,
                instrument_overrides={
                    symbol: {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "calendar_id": calendar_id,
                    }
                },
                symbol_cost_overrides={symbol: {"multiplier": 1.0}},
                warmup_periods=1,
            )
        )
        cached = _make_ohlcv_at([pd.Timestamp(cached_ts).to_pydatetime()])
        fetched = _make_ohlcv_at([pd.Timestamp(fetched_ts).to_pydatetime()])
        duration = pd.Timedelta(days=2) if timeframe == "D2" else pd.Timedelta(hours=1)
        cached["available_at"] = [pd.Timestamp(cached_ts) + duration]
        fetched["available_at"] = [pd.Timestamp(fetched_ts) + duration]

        with pytest.raises(ValueError, match=message):
            runner._merge_runtime_rows(symbol, cached, fetched)

        assert symbol not in runner._ohlcv_cache

    def test_same_timestamp_runtime_versions_match_database_policy(self):
        runner = self._make_runner(config=_test_cfg(warmup_periods=1))
        timestamp = datetime(2025, 1, 1, tzinfo=UTC)

        def version(close: float, available_at: str) -> pd.DataFrame:
            frame = _make_ohlcv_at([timestamp], price=close)
            frame["available_at"] = [pd.Timestamp(available_at)]
            return frame

        cached = version(200.0, "2025-01-01T02:00:00Z")
        older = runner._merge_runtime_rows(
            "BTCUSDT",
            cached,
            version(100.0, "2025-01-01T01:00:00Z"),
        )
        equal = runner._merge_runtime_rows(
            "BTCUSDT",
            older,
            version(150.0, "2025-01-01T02:00:00Z"),
        )
        newer = runner._merge_runtime_rows(
            "BTCUSDT",
            equal,
            version(300.0, "2025-01-01T03:00:00Z"),
        )

        assert older.loc[0, "close"] == 200.0
        assert equal.loc[0, "close"] == 200.0
        assert newer.loc[0, "close"] == 300.0
        assert newer.loc[0, "available_at"] == pd.Timestamp("2025-01-01T03:00:00Z")

    def test_poll_persists_only_newer_correction_without_replaying_observation(self):
        from librae.core.executor import execute_pending_decision_and_stops

        first_ts = datetime(2025, 1, 1, tzinfo=UTC)
        next_ts = datetime(2025, 1, 1, 1, tzinfo=UTC)

        def version(ts: datetime, close: float, available_at: str) -> pd.DataFrame:
            frame = _make_ohlcv_at([ts], price=close)
            frame["available_at"] = [pd.Timestamp(available_at)]
            return frame

        responses = iter(
            [
                version(first_ts, 100.0, "2025-01-01T01:00:00Z"),
                version(first_ts, 101.0, "2025-01-01T02:00:00Z"),
                version(first_ts, 150.0, "2025-01-01T02:00:00Z"),
                version(first_ts, 99.0, "2025-01-01T01:00:00Z"),
                version(next_ts, 200.0, "2025-01-01T02:00:00Z"),
            ]
        )
        feature_calls: list[pd.DataFrame] = []
        contexts: list[Context] = []

        def feature(history: pd.DataFrame) -> pd.DataFrame:
            feature_calls.append(history.copy())
            return _simple_feature_fn(history)

        class CaptureStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                contexts.append(ctx)
                return []

        runner = self._make_runner(
            strategy=CaptureStrategy(),
            fetcher=lambda *_args, **_kwargs: next(responses),
            feature_fn=feature,
            config=_test_cfg(warmup_periods=1),
            clock=lambda: datetime(2025, 1, 1, 10, tzinfo=UTC),
        )
        audit_events: list[tuple[str, str, datetime, dict[str, object], dict[str, str]]] = []

        def capture_audit(
            symbol: str,
            timeframe: str,
            bar: dict[str, object],
            ts: datetime,
        ) -> None:
            audit_events.append(
                (
                    symbol,
                    timeframe,
                    ts,
                    bar,
                    runner._market_data_subscriptions[symbol].to_dict(),
                )
            )

        runner._on_ohlcv = capture_audit
        with patch(
            "librae.live.engine.execute_pending_decision_and_stops",
            wraps=execute_pending_decision_and_stops,
        ) as execute:
            for _ in range(5):
                runner._poll_cycle()

        assert len(feature_calls) == 2
        assert len(contexts) == 2
        assert execute.call_count == 2
        assert runner._period_index == 2
        assert runner._last_bar_ts["BTCUSDT"] == next_ts
        assert [
            (symbol, timeframe, ts, bar["close"], bar["available_at"])
            for symbol, timeframe, ts, bar, _identity in audit_events
        ] == [
            ("BTCUSDT", "1h", first_ts, 100.0, pd.Timestamp("2025-01-01T01:00:00Z")),
            ("BTCUSDT", "1h", first_ts, 101.0, pd.Timestamp("2025-01-01T02:00:00Z")),
            ("BTCUSDT", "1h", next_ts, 200.0, pd.Timestamp("2025-01-01T02:00:00Z")),
        ]
        assert all(
            identity
            == {
                "symbol": "BTCUSDT",
                "timeframe": "H1",
                "calendar_id": "24/7",
                "session_mode": "extended",
                "data_source": "binance_spot",
                "instrument_type": "spot",
            }
            for *_event, identity in audit_events
        )

    def test_restart_replays_observed_history_only_to_audit_sink(self):
        from librae.core.executor import execute_pending_decision_and_stops

        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        history = _make_ohlcv_at([t0, t1])
        history.loc[0, "close"] = 101.0
        history["available_at"] = pd.to_datetime(["2025-01-01T01:30:00Z", "2025-01-01T02:00:00Z"])
        state_store = MemoryLiveStateStore()
        config = _test_cfg(warmup_periods=2)
        original = self._make_runner(config=config, state_store=state_store)
        original._last_bar_ts["BTCUSDT"] = t0
        original._last_cycle_ts = t0
        original._period_index = 1
        original._persist_state()

        feature_calls: list[pd.DataFrame] = []
        contexts: list[Context] = []

        def feature(frame: pd.DataFrame) -> pd.DataFrame:
            feature_calls.append(frame.copy())
            return _simple_feature_fn(frame)

        class CaptureStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                contexts.append(ctx)
                return []

        restored = self._make_runner(
            strategy=CaptureStrategy(),
            fetcher=lambda *_args, **_kwargs: history.copy(),
            feature_fn=feature,
            config=config,
            state_store=state_store,
            clock=lambda: datetime(2025, 1, 1, 10, tzinfo=UTC),
        )
        audit_events: list[tuple[datetime, float, object]] = []
        restored._on_ohlcv = lambda _symbol, _timeframe, bar, ts: audit_events.append(
            (ts, float(bar["close"]), bar["available_at"])
        )

        with patch(
            "librae.live.engine.execute_pending_decision_and_stops",
            wraps=execute_pending_decision_and_stops,
        ) as execute:
            restored._poll_cycle()

        assert audit_events == [
            (t0, 101.0, pd.Timestamp("2025-01-01T01:30:00Z")),
            (t1, 100.0, pd.Timestamp("2025-01-01T02:00:00Z")),
        ]
        assert len(feature_calls) == 1
        assert len(contexts) == 1
        assert contexts[0].ts == t1
        assert execute.call_count == 1
        assert restored._period_index == 2
        assert restored._last_bar_ts == {"BTCUSDT": t1}

    def test_restart_audit_replay_sorts_suffix_windows_on_coordinator_thread(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        timestamps = [t0 + timedelta(hours=offset) for offset in range(4)]
        histories = {
            symbol: _make_ohlcv_at(timestamps, price=price).assign(
                available_at=pd.DatetimeIndex(timestamps) + pd.Timedelta(hours=1)
            )
            for symbol, price in (("BBB", 200.0), ("AAA", 100.0))
        }
        requests: dict[str, list[int]] = {symbol: [] for symbol in histories}

        def fetcher(symbol, _timeframe, limit, **_kwargs):
            requests[symbol].append(limit)
            history = histories[symbol]
            return history.iloc[-2:].copy() if len(requests[symbol]) == 1 else history.copy()

        runner = self._make_runner(
            fetcher=fetcher,
            config=_test_cfg(
                symbols=["BBB", "AAA"],
                market_data_workers=2,
                warmup_periods=5,
            ),
            clock=lambda: datetime(2025, 1, 1, 10, tzinfo=UTC),
        )
        runner._last_bar_ts = {symbol: timestamps[-1] for symbol in histories}
        coordinator_thread = get_ident()
        audit_versions: list[tuple[int, str, datetime, object]] = []
        runner._on_ohlcv = lambda symbol, _timeframe, bar, ts: audit_versions.append(
            (get_ident(), symbol, ts, bar["available_at"])
        )

        runner._fetch_runtime_frames()

        assert requests == {"BBB": [6, 12, 24], "AAA": [6, 12, 24]}
        assert audit_versions == [
            (
                coordinator_thread,
                symbol,
                timestamp,
                pd.Timestamp(timestamp) + pd.Timedelta(hours=1),
            )
            for symbol in ("BBB", "AAA")
            for timestamp in timestamps
        ]

    def test_restart_audit_delivery_failure_is_not_implicitly_retried(self):
        timestamp = datetime(2025, 1, 1, tzinfo=UTC)
        history = _make_ohlcv_at([timestamp])
        history["available_at"] = [timestamp + timedelta(hours=1)]
        runner = self._make_runner(
            fetcher=lambda *_args, **_kwargs: history.copy(),
            config=_test_cfg(warmup_periods=1),
            clock=lambda: datetime(2025, 1, 1, 10, tzinfo=UTC),
        )
        runner._last_bar_ts["BTCUSDT"] = timestamp
        failing_sink = MagicMock(side_effect=RuntimeError("sink unavailable"))
        runner._on_ohlcv = failing_sink

        with pytest.raises(RuntimeError, match="sink unavailable"):
            runner._fetch_runtime_frames()

        retry_sink = MagicMock()
        runner._on_ohlcv = retry_sink
        runner._fetch_runtime_frames()

        failing_sink.assert_called_once()
        retry_sink.assert_not_called()

    def test_poll_slower_than_timeframe_warns(self, caplog):
        with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
            self._make_runner(config=_test_cfg(poll_seconds=3601))

        assert "poll_seconds=3601 exceeds timeframe=H1" in caplog.text

    def test_cycle_diagnostics_marks_poll_deadline_miss(self):
        runner = self._make_runner(config=_test_cfg(poll_seconds=1))

        runner._finish_cycle_diagnostics(TEST_CLOCK_NOW, perf_counter() - 2.0)

        diagnostics = runner.last_cycle_diagnostics
        assert diagnostics is not None
        assert diagnostics.deadline_missed is True
        assert diagnostics.cycle_seconds >= 2.0

    def test_live_without_default_db_requires_explicit_state_store(self):
        with pytest.raises(ValueError, match="requires an explicit durable state_store"):
            LiveTrader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(mode="live"),
                adapter=lambda *args, **kwargs: _make_ohlcv_df(),
                order_adapter=_mock_order_adapter(),
                cost_model=_zero_cost_model(),
                runtime_revision="test-runtime",
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

    def test_sim_open_volume_limit_uses_decision_bar_volume(self):
        class BuyOnce(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=100.0)]
                return []

        first = _make_ohlcv_df(start_hour=0)
        first.loc[first.index[-1], "volume"] = 20.0
        second = _make_ohlcv_df(start_hour=1)
        second.loc[second.index[-2], "volume"] = 20.0
        second.loc[second.index[-1], "volume"] = 2_000.0
        frames = iter((first, second))
        runner = self._make_runner(
            strategy=BuyOnce(),
            fetcher=lambda *args, **kwargs: next(frames),
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=0.5,
                    warmup_periods=5,
                )
            ),
        )

        runner.run(max_iterations=2)

        assert runner._positions["BTCUSDT"].quantity == pytest.approx(10.0)

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

    def test_same_event_retry_does_not_rollback_strategy_instance_mutation(self):
        class MutatesBeforeFirstFailure(Strategy):
            def __init__(self):
                self.attempts = 0
                self.contexts: list[tuple[datetime, int]] = []

            def on_bar(self, ctx: Context):
                self.attempts += 1
                self.contexts.append((ctx.ts, ctx.period_index))
                if self.attempts == 1:
                    raise RuntimeError("retry me")
                return []

        strategy = MutatesBeforeFirstFailure()
        runner = self._make_runner(strategy=strategy)

        runner.run(max_iterations=2)

        assert strategy.attempts == 2
        assert strategy.contexts[0] == strategy.contexts[1]
        assert strategy.contexts[0][1] == 0
        assert runner._period_index == 1

    def test_restart_restores_engine_index_but_not_strategy_instance_state(self):
        store = MemoryLiveStateStore()
        config = _test_cfg()

        class StatefulCounter(Strategy):
            def __init__(self):
                self.count = 0
                self.seen_periods: list[int] = []

            def on_bar(self, ctx: Context):
                self.count += 1
                self.seen_periods.append(ctx.period_index)
                return []

        first_strategy = StatefulCounter()
        first = self._make_runner(
            strategy=first_strategy,
            fetcher=lambda *args, **kwargs: _make_ohlcv_df(start_hour=0),
            state_store=store,
            config=config,
        )
        first.run(max_iterations=1)

        restarted_strategy = StatefulCounter()
        restarted = self._make_runner(
            strategy=restarted_strategy,
            fetcher=lambda *args, **kwargs: _make_ohlcv_df(start_hour=1),
            state_store=store,
            config=config,
        )
        restarted.run(max_iterations=1)

        assert first_strategy.count == 1
        assert restarted_strategy.count == 1
        assert restarted_strategy.seen_periods == [1]
        assert restarted._period_index == 2

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

        with pytest.raises(ValueError, match=r"timestamp must be.*timezone-aware"):
            runner._eligible_runtime_rows("BTCUSDT", frame)

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
            config=_test_cfg(mode=mode, symbols=["AAA", "BBB"], warmup_periods=1),
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
            config=_test_cfg(symbols=["AAA", "BBB"], warmup_periods=1),
        )

        runner._poll_cycle()
        runner._poll_cycle()

        contexts = [call.args[0] for call in strategy.on_bar.call_args_list]
        assert [ctx.ts for ctx in contexts] == [t0, t1, t1]
        assert [set(ctx.bars) for ctx in contexts] == [
            {"BBB"},
            {"AAA"},
            {"AAA", "BBB"},
        ]

    def test_unready_grouped_decision_does_not_block_an_unrelated_symbol(self):
        """A strategy managing a spread (NEAR/NEXT) and an independent
        symbol (SOLO) must not have SOLO's decision blocked just because
        the spread's data isn't complete yet — the strategy self-checks
        readiness via ctx.available_symbols (as examples/multi_leg_spread
        does) instead of the engine queueing an incomplete decision."""
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t1 + timedelta(hours=1)
        responses = {
            "NEAR": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1]), _make_ohlcv_at([t2])]),
            "NEXT": iter([_make_ohlcv_at([t1]), _make_ohlcv_at([t1]), _make_ohlcv_at([t2])]),
            "SOLO": iter([_make_ohlcv_at([t0]), _make_ohlcv_at([t1]), _make_ohlcv_at([t2])]),
        }

        def fetcher(symbol, *_args, **_kwargs):
            return next(responses[symbol])

        class SpreadPlusSolo(Strategy):
            """This test exercises the sim-mode path (see
            test_live_group_leg_rejection_cancels_only_that_group for the
            broker-facing serial-submission/per-group-failure behavior)."""

            def __init__(self):
                self.solo_emitted = False

            def on_bar(self, ctx):
                if {"NEAR", "NEXT"}.issubset(ctx.available_symbols):
                    return [
                        OrderIntent(action="long", symbol="NEAR", quantity=1.0, group_id="spread"),
                        OrderIntent(action="short", symbol="NEXT", quantity=1.0, group_id="spread"),
                    ]
                if not self.solo_emitted and "SOLO" in ctx.available_symbols:
                    self.solo_emitted = True
                    return [OrderIntent(action="long", symbol="SOLO", quantity=1.0)]
                return []

        runner = self._make_runner(
            strategy=SpreadPlusSolo(),
            fetcher=fetcher,
            config=_test_cfg(
                symbols=["NEAR", "NEXT", "SOLO"],
                warmup_periods=1,
            ),
        )

        runner._poll_cycle()  # t0: NEXT has no bar yet, spread withheld, SOLO decided
        assert runner._pending_decision == [OrderIntent(action="long", symbol="SOLO", quantity=1.0)]

        runner._poll_cycle()  # t1: SOLO fills; NEXT now has a bar, spread decided
        assert runner._positions["SOLO"].quantity == pytest.approx(1.0)
        assert "NEAR" not in runner._positions
        assert "NEXT" not in runner._positions

        runner._poll_cycle()  # t2: spread fills
        assert runner._positions["NEAR"].side == "long"
        assert runner._positions["NEAR"].quantity == pytest.approx(1.0)
        assert runner._positions["NEXT"].side == "short"
        assert runner._positions["NEXT"].quantity == pytest.approx(1.0)

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
            config=_test_cfg(symbols=["AAA", "BBB"], warmup_periods=1),
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

    def test_duplicate_broker_bars_fail_closed_before_strategy(self):
        ts = datetime(2025, 1, 1, tzinfo=UTC)
        duplicated = _make_ohlcv_at([ts, ts])

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *_args, **_kwargs: duplicated,
            config=_test_cfg(symbols=["AAA", "BBB"], warmup_periods=1),
        )

        with pytest.raises(ValueError, match="unique bar timestamps"):
            runner._eligible_runtime_rows("AAA", duplicated)

        strategy.on_bar.assert_not_called()

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
            config=_test_cfg(symbols=["AAA", "BBB"], warmup_periods=1),
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
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        timestamps = [now - timedelta(hours=2), now - timedelta(hours=1), now]
        frame = _make_ohlcv_at(timestamps)
        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *_args, **_kwargs: frame,
            config=_test_cfg(mode=mode, warmup_periods=1),
            order_adapter=_mock_order_adapter() if mode == "live" else None,
            clock=lambda: now + timedelta(hours=1),
        )
        runner._last_bar_ts["BTCUSDT"] = now - timedelta(hours=3)
        runner._last_cycle_ts = now - timedelta(hours=3)

        runner._poll_cycle()

        assert strategy.on_bar.call_count == expected_events
        if mode == "live":
            assert strategy.on_bar.call_args.args[0].ts == now

    def test_live_event_contains_only_symbols_selected_at_that_timestamp(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        t2 = t1 + timedelta(hours=1)
        frames = {
            "AAA": _make_ohlcv_at([t0, t1, t2]),
            "BBB": _make_ohlcv_at([t0, t1]),
        }
        contexts: list[Context] = []

        class RejectHistoricalDecision(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                contexts.append(ctx)
                if ctx.ts == t1 and "AAA" in ctx.available_symbols:
                    return [OrderIntent(action="long", symbol="AAA", quantity=1.0)]
                return []

        order_adapter = _mock_order_adapter()
        runner = self._make_runner(
            strategy=RejectHistoricalDecision(),
            fetcher=lambda symbol, *_args, **_kwargs: frames[symbol],
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                warmup_periods=2,
            ),
            order_adapter=order_adapter,
        )

        runner._poll_cycle()

        assert [ctx.ts for ctx in contexts] == [t1, t2]
        assert [set(ctx.bars) for ctx in contexts] == [{"BBB"}, {"AAA"}]
        assert runner._pending_decision == []
        order_adapter.place_order.assert_not_called()

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
                return PortfolioWeights(weights={"AAA": 0.6, "BBB": 0.4})

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
            config=_test_cfg(mode=mode, symbols=["AAA", "BBB"], warmup_periods=1),
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

        assert calls[0]["limit"] == 6  # warmup plus one possibly forming bar
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

    def test_entry_and_exit_use_injected_notifier(self):
        call_num = 0
        notifier = MagicMock(enabled=True)

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            notifier=notifier,
        )
        runner.run(max_iterations=3)

        assert notifier.send_signal.call_count == 1
        notifier.send_signal.assert_called_once_with(
            strategy="test",
            symbol="BTCUSDT",
            side="LONG",
            price=103.5,
            quantity=1.0,
            notional=103.5,
        )
        assert notifier.send_exit.call_count == 1
        exit_call = notifier.send_exit.call_args
        assert exit_call.kwargs["strategy"] == "test"
        assert exit_call.kwargs["symbol"] == "BTCUSDT"
        assert exit_call.kwargs["side"] == "long"
        assert exit_call.kwargs["exit_price"] == 103.5

    def test_open_calls_notify_entry(self):
        call_num = 0
        notifier = MagicMock(enabled=True)

        def fetcher(*args, **kwargs):
            nonlocal call_num
            call_num += 1
            return _make_ohlcv_df(n=5, start_hour=call_num)

        runner = self._make_runner(
            strategy=_AlwaysBuyStrategy(),
            fetcher=fetcher,
            notifier=notifier,
        )
        runner.run(max_iterations=2)

        notifier.send_signal.assert_called_once_with(
            strategy="test",
            symbol="BTCUSDT",
            side="LONG",
            price=103.5,
            quantity=1.0,
            notional=103.5,
        )

    def test_status_interval_requires_notifier(self):
        with pytest.raises(ValueError, match="requires a notifier"):
            self._make_runner(status_interval_periods=12)

    @pytest.mark.parametrize("interval", [True, 0, -1, 1.5])
    def test_status_interval_must_be_positive_integer(self, interval):
        with pytest.raises(ValueError, match="positive integer"):
            self._make_runner(
                notifier=MagicMock(enabled=True),
                status_interval_periods=interval,
            )

    def test_status_notification_accumulates_pnl_over_the_full_window(self):
        """period_pnl must be the change over all `num_periods` bars since
        the last status notification, not just the most recent bar — a
        constant per-bar accounting update (like _prev_equity) must not
        reset the window early."""
        notifier = MagicMock(enabled=True)
        runner = self._make_runner(
            notifier=notifier,
            status_interval_periods=3,
        )
        equity_sequence = iter([100_010.0, 100_005.0, 100_030.0, 999_999.0])
        runner._calc_account_snapshot = lambda: (next(equity_sequence), {})

        ts = datetime(2025, 1, 1, tzinfo=UTC)
        for _ in range(3):
            runner._record_equity(ts, {})

        notifier.send_status.assert_called_once()
        kwargs = notifier.send_status.call_args.kwargs
        assert kwargs["period_pnl"] == pytest.approx(100_030.0 - 100_000.0)
        assert kwargs["num_periods"] == 3

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
                    return PortfolioWeights(weights={"AAA": 0.6, "BBB": 0.4})
                return []

        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(order_id="aaa", quantity=600.0, average=100.0, executed_at=t0),
            {"id": "", "status": "rejected"},
        ]
        runner = self._make_runner(
            strategy=AllocateOnce(),
            fetcher=fetcher,
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                warmup_periods=1,
            ),
            order_adapter=adapter,
        )

        runner.run(max_iterations=1)

        assert runner._halted is True
        assert set(runner._positions) == {"AAA"}
        assert runner._positions["AAA"].quantity == pytest.approx(600.0)

    def test_live_mode_without_order_adapter_raises(self):
        cfg = _test_cfg(mode="live")
        with pytest.raises(ValueError, match="requires an explicit order_adapter"):
            self._make_runner(config=cfg)

    def test_adv_limit_accepts_intraday_runtime_with_registered_calendar(self):
        config = _test_cfg(
            execution=ExecutionPolicy(
                adv_lookback_sessions=20,
                max_adv_participation_rate=0.01,
            )
        )

        assert config.execution.adv_lookback_sessions == 20

    def test_first_run_nonflat_broker_state_halts_without_adoption(self):
        """A position without a durable cash ledger cannot seed live equity."""
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
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._positions == {}
        assert any("Non-flat First-run Broker State" in item[1]["title"] for item in alerts)

    def test_first_run_short_broker_state_halts_without_adoption(self):
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

        assert runner._halted is True
        assert runner._positions == {}

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

    def test_position_reconciliation_detects_changed_average_cost(self):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 2.0,
            "avg_price": 101.0,
            "unrealized_pnl": 0.0,
        }
        runner = self._make_runner(
            strategy=_HoldStrategy(),
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )
        local = {
            "BTCUSDT": PositionState(
                symbol="BTCUSDT",
                side="long",
                entry_price=100.0,
                quantity=2.0,
                entry_at=TEST_CLOCK_NOW,
                periods_held=1,
                entry_commission=0.0,
                entry_slippage=0.0,
                entry_tax=0.0,
                total_entry_cost=200.0,
            )
        }

        assert runner._position_books_match(local, runner._read_broker_positions()) is False

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

    @pytest.mark.parametrize(
        ("broker_position", "message"),
        [
            ({"symbol": "BTCUSDT", "avg_price": 100.0}, "missing size"),
            (
                {"symbol": "BTCUSDT", "size": float("nan"), "avg_price": 100.0},
                "non-finite size",
            ),
            (
                {"symbol": "BTCUSDT", "size": 1.0, "avg_price": "not-a-number"},
                "invalid average price",
            ),
            (
                {"symbol": "BTCUSDT", "size": 1.0, "avg_price": 0.0},
                "invalid average price",
            ),
        ],
    )
    def test_position_reconciliation_rejects_missing_broker_facts(
        self,
        broker_position,
        message,
    ):
        mock_order_adapter = _mock_order_adapter()
        mock_order_adapter.get_position.return_value = broker_position
        runner = self._make_runner(
            config=_test_cfg(mode="live"),
            order_adapter=mock_order_adapter,
        )

        with pytest.raises(ValueError, match=message):
            runner._read_broker_positions()

    def test_position_reconciliation_accepts_missing_average_price(self):
        """CCXT spot balances never carry avg_price (no cost-basis field in
        the balance API) — a missing avg_price is not itself an error,
        unlike a missing size which every broker returns."""
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

        positions = runner._read_broker_positions()

        assert positions["BTCUSDT"].average_price is None

    def test_ibkr_reconciliation_uses_execution_broker_not_data_adapter(self):
        mock_order_adapter = _mock_order_adapter()
        config = _test_cfg(
            mode="live",
            broker="ibkr",
            symbols=["ES"],
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={
                "ES": {
                    "instrument_type": "contract_quarterly",
                    "currency": "USD",
                    "market": "us_equity",
                    "data_source": "binance_spot",
                    "data_adapter": "crypto",
                    "security_type": "FUT",
                    "exchange": "CME",
                    "contract_month": "202609",
                }
            },
            symbol_cost_overrides={"ES": {"multiplier": 50.0}},
        )
        runner = self._make_runner(
            strategy=_HoldStrategy(),
            config=config,
            order_adapter=mock_order_adapter,
        )

        assert runner._read_broker_positions() == {}
        mock_order_adapter.get_position.assert_called_once_with(
            PositionRequest(
                symbol="ES",
                venue_symbol="ES",
                currency="USD",
                multiplier=1.0,
                security_type="FUT",
                exchange="CME",
                contract_month="202609",
            )
        )

    def test_cash_drift_beyond_tolerance_alerts_without_adjusting_cash(self):
        """Drift past CASH_RECONCILE_TOLERANCE_PCT must alert with both
        numbers but never mutate local cash — reconciliation is alert-only,
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
        assert runner._cash == 100_000.0

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

    def test_adapter_without_get_balance_reports_unavailable_reconciliation(self, caplog):
        """An optional capability stays non-fatal but cannot disappear silently."""
        mock_order_adapter = _mock_order_adapter()
        del mock_order_adapter.get_balance

        cfg = _test_cfg(mode="live", symbols=["BTC/USDT"])
        runner = self._make_runner(config=cfg, order_adapter=mock_order_adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
            runner.run(max_iterations=1)  # must not raise

        assert not [
            kw for m, kw in alerts if m == "send_alert" and "Cash Reconciliation" in kw["title"]
        ]
        assert "Cash reconciliation unavailable" in caplog.text

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
        runner.STALE_DATA_TOLERANCE_BARS = 2
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
        runner.STALE_DATA_TOLERANCE_BARS = 2

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
        runner.STALE_DATA_TOLERANCE_BARS = 2
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

        def on_position_event(event, _sequence):
            if event.event_type == "open":
                fill_prices.append(event.price)

        runner = self._make_runner(
            strategy=BuyOnceStrategy(),
            fetcher=fetcher,
            feature_fn=flaky_feature_fn,
        )
        runner._on_position_event = on_position_event
        runner.run(max_iterations=3)

        # bar1 (level=100): strategy queues a buy. bar2 (level=200): feature_fn
        # raises, but the pending buy must still fill at bar2's own price
        # (open=199.5) -- not silently deferred to bar3's price (299.5).
        assert fill_prices == [199.5]

    @pytest.mark.parametrize(
        ("shape", "message"),
        [
            ("not_dataframe", "pandas DataFrame"),
            ("empty", "must not be empty"),
            ("not_datetime_index", "DatetimeIndex"),
            ("naive", "timezone-aware"),
            ("descending", "strictly increasing"),
            ("duplicate", "unique"),
            ("future", "after event"),
            ("stale", "final timestamp"),
        ],
    )
    def test_feature_output_must_match_current_event(self, shape: str, message: str):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)

        def malformed_feature(history: pd.DataFrame):
            if shape == "not_dataframe":
                return history.iloc[-1]
            if shape == "empty":
                return history.iloc[0:0]
            if shape == "not_datetime_index":
                return history.reset_index(drop=True)
            if shape == "naive":
                result = history.copy()
                result.index = result.index.tz_localize(None)
                return result
            if shape == "descending":
                return history.iloc[::-1]
            if shape == "duplicate":
                return pd.concat([history, history.iloc[[-1]]])
            if shape == "future":
                future = history.iloc[[-1]].copy()
                future.index = pd.DatetimeIndex([history.index[-1] + pd.Timedelta(hours=1)])
                return pd.concat([history, future])
            return history.iloc[:-1]

        strategy = MagicMock(spec=Strategy)
        strategy.on_bar.return_value = []
        runner = self._make_runner(
            strategy=strategy,
            fetcher=lambda *_args, **_kwargs: _make_ohlcv_at([t0, t1]),
            feature_fn=malformed_feature,
            config=_test_cfg(warmup_periods=2),
        )

        with pytest.raises((TypeError, ValueError), match=message):
            runner._poll_cycle()

        strategy.on_bar.assert_not_called()
        assert runner._last_bar_ts == {}
        assert runner._last_cycle_ts is None

    def test_invalid_feature_output_retries_without_replaying_pending_fill(self):
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        responses = iter(
            [
                _make_ohlcv_at([t0], price=100.0),
                _make_ohlcv_at([t1], price=200.0),
                _make_ohlcv_at([t1], price=200.0),
            ]
        )
        feature_calls = 0
        strategy_calls = 0
        opened = []
        state_store = MemoryLiveStateStore()

        def feature_fn(history: pd.DataFrame) -> pd.DataFrame:
            nonlocal feature_calls
            feature_calls += 1
            if feature_calls == 2:
                return history.iloc[:-1]
            return _simple_feature_fn(history)

        class BuyOnceStrategy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                nonlocal strategy_calls
                strategy_calls += 1
                if ctx.symbol not in ctx.positions:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        runner = self._make_runner(
            strategy=BuyOnceStrategy(),
            fetcher=lambda *_args, **_kwargs: next(responses),
            feature_fn=feature_fn,
            state_store=state_store,
            config=_test_cfg(warmup_periods=1),
        )
        runner._on_position_event = lambda event, _sequence: (
            opened.append(event) if event.event_type == "open" else None
        )

        runner._poll_cycle()
        with pytest.raises(ValueError, match="must not be empty"):
            runner._poll_cycle()

        assert runner._last_bar_ts == {"BTCUSDT": t0}
        assert len(opened) == 1
        assert runner._positions["BTCUSDT"].quantity == pytest.approx(1.0)
        persisted = state_store.load(runner._state_key)
        assert persisted is not None
        assert persisted.last_bar_ts == {"BTCUSDT": t0}
        assert persisted.positions["BTCUSDT"].quantity == pytest.approx(1.0)

        runner._poll_cycle()

        assert runner._last_bar_ts == {"BTCUSDT": t1}
        assert len(opened) == 1
        assert feature_calls == 3
        assert strategy_calls == 2

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

    def test_max_drawdown_breach_flattens_and_halts_account(self):
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
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=5,
            ),
            risk=RiskPolicy(max_drawdown_rate=0.2),
        )
        runner = self._make_runner(strategy=BuyOnceStrategy(), fetcher=fetcher, config=cfg)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        # bar1: buy queued. bar2: buy fills (~all cash). bar3: price craters
        # -> breach detected before the strategy sees the bar -> flattened +
        # account-halted. bar4 confirms there is no re-buy or duplicate alert.
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
        runner._on_bar = lambda *args: callbacks.append((args[4], args[5], args[6]))

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


def _make_fill_event() -> PositionEvent:
    return PositionEvent(
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
        runtime_revision: str = "test-runtime",
        clock=None,
        on_ready=None,
        on_runtime_event=None,
    ) -> LiveTrader:
        trader = LiveTrader(
            strategy,
            _simple_feature_fn,
            config=config or _test_cfg(mode="live"),
            adapter=lambda *a, **kw: _make_ohlcv_df(),
            cost_model=_zero_cost_model(),
            order_adapter=adapter,
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            state_store=state_store or MemoryLiveStateStore(),
            runtime_revision=runtime_revision,
            clock=clock or (lambda: TEST_CLOCK_NOW),
            on_ready=on_ready,
            on_runtime_event=on_runtime_event,
        )
        trader.STALE_DATA_TOLERANCE_BARS = 100
        return trader

    def test_live_runtime_revision_is_required_before_checkpoint_or_broker_access(self):
        adapter = _mock_order_adapter()
        store = MagicMock()

        with pytest.raises(ValueError, match="runtime_revision"):
            LiveTrader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(mode="live"),
                adapter=lambda *args, **kwargs: _make_ohlcv_df(),
                order_adapter=adapter,
                cost_model=_zero_cost_model(),
                state_store=store,
            )

        store.load.assert_not_called()
        adapter.get_position.assert_not_called()
        adapter.get_balance.assert_not_called()
        adapter.find_order.assert_not_called()
        adapter.list_open_orders.assert_not_called()
        adapter.place_order.assert_not_called()

    def test_runtime_revision_mismatch_preserves_checkpoint_and_old_revision_rolls_back(self):
        store = MemoryLiveStateStore()
        first = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            runtime_revision="revision-a",
        )
        first._active_orders = [
            TrackedOrder(
                request=OrderRequest(
                    client_order_id="pending-1",
                    symbol="BTCUSDT",
                    side="buy",
                    quantity=1.0,
                    order_type="market",
                    submitted_at=TEST_CLOCK_NOW,
                ),
                placement_attempted=True,
                placement_attempted_at=TEST_CLOCK_NOW,
                order_id="broker-1",
                status="submitted",
            )
        ]
        first._persist_state()
        checkpoint_before = store.load(first._state_key)
        assert checkpoint_before is not None

        new_adapter = _mock_order_adapter()
        with pytest.raises(RuntimeError, match=r"checkpoint='revision-a'.*requested='revision-b'"):
            self._make_trader(
                _HoldStrategy(),
                new_adapter,
                state_store=store,
                runtime_revision="revision-b",
            )

        checkpoint_after = store.load(first._state_key)
        assert checkpoint_after == checkpoint_before
        assert first._state_key.endswith(first._config.config_hash)
        new_adapter.get_position.assert_not_called()
        new_adapter.get_balance.assert_not_called()
        new_adapter.find_order.assert_not_called()
        new_adapter.list_open_orders.assert_not_called()
        new_adapter.place_order.assert_not_called()

        rollback = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            runtime_revision="revision-a",
        )

        assert rollback.run_id == first.run_id
        assert rollback._active_orders == first._active_orders
        assert rollback._snapshot_state().runtime_revision == "revision-a"
        assert store.load(first._state_key) == checkpoint_before

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

    def test_target_rebalance_replans_from_actual_fill_before_next_symbol(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(
                order_id="aaa-open",
                quantity=500.0,
                average=200.0,
                executed_at=TEST_CLOCK_NOW,
            ),
            {
                "id": "aaa-reduce",
                "status": "submitted",
                "amount": 250.0,
                "filled": 0.0,
            },
        ]
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["AAA", "BBB"]),
        )
        runner._last_prices = {"AAA": 100.0, "BBB": 100.0}

        complete = runner._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            {
                "AAA": {"close": 100.0, "volume": 10_000.0},
                "BBB": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is False
        second = adapter.place_order.call_args_list[1].args[0]
        assert second["symbol"] == "AAA"
        assert second["side"] == "sell"
        assert second["quantity"] == pytest.approx(250.0)

    def test_delayed_target_waits_for_fresh_prices_before_next_leg(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = lambda signal: {
            "id": f"order-{adapter.place_order.call_count}",
            "status": "accepted",
            "amount": signal["quantity"],
            "filled": 0.0,
        }
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["AAA", "BBB"]),
        )
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        runner._last_prices = {"AAA": 100.0, "BBB": 100.0}

        complete = runner._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            {
                "AAA": {"close": 100.0, "volume": 10_000.0},
                "BBB": {"close": 100.0, "volume": 10_000.0},
            },
            t0,
        )

        assert complete is False
        first = adapter.place_order.call_args_list[0].args[0]
        assert (first["canonical_symbol"], first["side"], first["quantity"]) == (
            "AAA",
            "buy",
            pytest.approx(500.0),
        )
        adapter.get_order.return_value = _broker_report(
            order_id="order-1",
            quantity=500.0,
            average=100.0,
            executed_at=t1,
        )

        runner._advance_live_orders()

        assert runner._active_orders == []
        assert runner._live_rebalance is not None
        assert adapter.place_order.call_count == 1

        runner._process_cycle(
            {
                "AAA": _make_ohlcv_at([t0, t1], price=200.0),
                "BBB": _make_ohlcv_at([t0, t1], price=50.0),
            },
            t1,
        )

        second = adapter.place_order.call_args_list[1].args[0]
        assert (second["canonical_symbol"], second["side"], second["quantity"]) == (
            "AAA",
            "sell",
            pytest.approx(125.0),
        )
        assert runner._active_orders[0].request.submitted_at == t1

    def test_live_target_uses_fresh_per_bar_volume_budget(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=f"order-{adapter.place_order.call_count}",
            quantity=signal["quantity"],
            average=100.0,
        )
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=0.1,
                    max_rebalance_delay_bars=2,
                    warmup_periods=5,
                ),
            ),
        )
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        runner._last_prices = {"BTCUSDT": 100.0}

        complete = runner._execute_live_decision(
            PortfolioWeights(weights={"BTCUSDT": 0.5}),
            {"BTCUSDT": {"close": 100.0, "volume": 10.0}},
            t0,
        )

        assert complete is False
        assert adapter.place_order.call_args_list[0].args[0]["quantity"] == pytest.approx(1.0)
        assert runner._live_rebalance is not None
        assert runner._live_rebalance.delay_bars == 1

        runner._continue_live_rebalance(
            {"BTCUSDT": {"close": 100.0, "volume": 20.0}},
            t1,
        )

        assert adapter.place_order.call_args_list[1].args[0]["quantity"] == pytest.approx(2.0)
        assert runner._live_rebalance is not None
        assert runner._live_rebalance.execution_bar_ts == t1
        assert runner._live_rebalance.filled_bar_quantity_by_symbol == {"BTCUSDT": 2.0}

    def test_unavailable_live_target_is_atomic_and_bounded(self):
        adapter = _mock_order_adapter()
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    max_rebalance_delay_bars=1,
                    warmup_periods=5,
                ),
            ),
            on_runtime_event=runtime_events.append,
        )
        bars = {
            "AAA": {
                "close": 100.0,
                "volume": 10_000.0,
                "can_buy": True,
                "can_sell": True,
            },
            "BBB": {
                "close": 100.0,
                "volume": 10_000.0,
                "can_buy": False,
                "can_sell": True,
            },
        }

        complete = runner._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            bars,
            TEST_CLOCK_NOW,
        )

        assert complete is False
        assert runner._halted is False
        assert runner._live_rebalance is not None
        adapter.place_order.assert_not_called()
        assert runtime_events[-1].detail["reason"] == "rebalance_deferred"
        assert runtime_events[-1].detail["symbols"] == ["BBB"]

        runner._continue_live_rebalance(bars, TEST_CLOCK_NOW + timedelta(hours=1))

        assert runner._halted is True
        adapter.place_order.assert_not_called()

    def test_live_target_missing_coherent_snapshot_remains_pending(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "aaa",
            "status": "accepted",
            "amount": 500.0,
            "filled": 0.0,
        }
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    max_rebalance_delay_bars=2,
                    warmup_periods=5,
                ),
            ),
        )
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        runner._last_prices = {"AAA": 100.0, "BBB": 100.0}
        runner._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            {
                "AAA": {"close": 100.0, "volume": 10_000.0},
                "BBB": {"close": 100.0, "volume": 10_000.0},
            },
            t0,
        )
        adapter.get_order.return_value = _broker_report(
            order_id="aaa",
            quantity=500.0,
            average=100.0,
        )
        runner._advance_live_orders()

        runner._continue_live_rebalance(
            {"AAA": {"close": 110.0, "volume": 10_000.0}},
            t0 + timedelta(hours=1),
        )

        assert runner._halted is False
        assert runner._active_orders == []
        assert runner._live_rebalance is not None
        assert runner._live_rebalance.delay_bars == 1
        assert adapter.place_order.call_count == 1

    def test_live_target_replans_after_adv_session_reset(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "aaa",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                timeframe="D1",
                symbols=["AAA"],
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    adv_lookback_sessions=1,
                    max_adv_participation_rate=0.1,
                    max_rebalance_delay_bars=2,
                    warmup_periods=5,
                ),
            ),
        )
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(days=1)
        runner._last_prices = {"AAA": 100.0}
        runner._adv_session_labels = {"AAA": t0.date().isoformat()}
        runner._adv_filled_quantities = {"AAA": 1.0}

        complete = runner._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5}),
            {"AAA": {"close": 100.0, "volume": 10.0}},
            t0,
            lagged_adv_by_symbol={"AAA": 10.0},
        )

        assert complete is False
        assert runner._live_rebalance is not None
        assert runner._live_rebalance.delay_bars == 1
        adapter.place_order.assert_not_called()

        frame = pd.DataFrame(
            {
                "ts": pd.DatetimeIndex([t0, t1]),
                "open": [100.0, 100.0],
                "high": [101.0, 101.0],
                "low": [99.0, 99.0],
                "close": [100.0, 100.0],
                "volume": [10.0, 20.0],
            }
        )
        runner._process_cycle({"AAA": frame}, t1)

        request = adapter.place_order.call_args.args[0]
        assert request["quantity"] == pytest.approx(1.0)
        assert runner._adv_session_labels == {"AAA": t1.date().isoformat()}
        assert runner._adv_filled_quantities == {"AAA": 0.0}

    def test_restored_live_target_waits_for_a_fresh_completed_bar(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = lambda signal: {
            "id": f"order-{adapter.place_order.call_count}",
            "status": "accepted",
            "amount": signal["quantity"],
            "filled": 0.0,
        }
        config = _test_cfg(
            mode="live",
            symbols=["AAA", "BBB"],
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                max_rebalance_delay_bars=2,
                warmup_periods=5,
            ),
        )
        t0 = datetime(2025, 1, 1, tzinfo=UTC)
        t1 = t0 + timedelta(hours=1)
        first = self._make_trader(
            _HoldStrategy(),
            adapter,
            state_store=store,
            config=config,
        )
        first._last_prices = {"AAA": 100.0, "BBB": 100.0}
        first._execute_live_decision(
            PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            {
                "AAA": {"close": 100.0, "volume": 10_000.0},
                "BBB": {"close": 100.0, "volume": 10_000.0},
            },
            t0,
        )
        adapter.get_order.return_value = _broker_report(
            order_id="order-1",
            quantity=500.0,
            average=100.0,
            executed_at=t1,
        )
        adapter.get_position.side_effect = lambda request: {
            "symbol": request.venue_symbol,
            "size": 500.0 if request.symbol == "AAA" else 0.0,
            "avg_price": 100.0,
        }
        adapter.get_balance.return_value = {"total": 50_000.0}

        restored = self._make_trader(
            _HoldStrategy(),
            adapter,
            state_store=store,
            config=config,
        )
        restored._initialize_run()

        assert restored._halted is False
        assert restored._active_orders == []
        assert restored._live_rebalance is not None
        assert adapter.place_order.call_count == 1

        restored._process_cycle(
            {
                "AAA": _make_ohlcv_at([t0, t1]),
                "BBB": _make_ohlcv_at([t0, t1]),
            },
            t1,
        )

        second = adapter.place_order.call_args_list[1].args[0]
        assert (second["canonical_symbol"], second["side"], second["quantity"]) == (
            "BBB",
            "buy",
            pytest.approx(500.0),
        )
        assert restored._active_orders[0].request.submitted_at == t1

    def test_live_group_leg_rejection_cancels_only_that_group(self):
        """No broker adapter offers a native combo order, so live submits each
        leg of a group serially. A rejected leg cancels that group's other
        legs and alerts — it does not halt the whole account, since the
        failure has nothing to do with unrelated groups or independent
        intents (see test_live_group_failure_does_not_block_other_groups)."""
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(order_id="spot-1", status="filled", quantity=1.0, average=100.0),
            _broker_report(order_id="perp-1", status="rejected", quantity=1.0, filled=0.0),
        ]
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["SPOT", "PERP"]),
        )
        alerts = []
        events: list[PositionEvent] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner._on_position_event = lambda event, _sequence: events.append(event)
        runner._last_prices = {"SPOT": 100.0, "PERP": 100.0}

        complete = runner._execute_live_decision(
            [
                OrderIntent(
                    action="long", symbol="SPOT", quantity=1.0, reason="basis", group_id="basis"
                ),
                OrderIntent(
                    action="short", symbol="PERP", quantity=1.0, reason="basis", group_id="basis"
                ),
            ],
            {
                "SPOT": {"close": 100.0, "volume": 10_000.0},
                "PERP": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert runner._active_orders == []
        assert runner._positions["SPOT"].quantity == pytest.approx(1.0)
        assert runner._positions["SPOT"].group_id == "basis"
        assert "PERP" not in runner._positions
        assert [(event.symbol, event.group_id) for event in events] == [("SPOT", "basis")]
        assert any(m == "send_alert" and "'basis'" in kw["message"] for m, kw in alerts)

    def test_live_group_failure_does_not_block_other_groups(self):
        """A failed group's rejection only cancels its own siblings — an
        unrelated group and an independent (ungrouped) intent queued in the
        same decision keep executing normally."""
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = [
            _broker_report(order_id="a-1", status="rejected", quantity=1.0, filled=0.0),
            _broker_report(order_id="c-1", status="filled", quantity=1.0, average=100.0),
            _broker_report(order_id="d-1", status="filled", quantity=1.0, average=100.0),
            _broker_report(order_id="solo-1", status="filled", quantity=1.0, average=100.0),
        ]
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "B", "C", "D", "SOLO"]),
        )
        runner._last_prices = {symbol: 100.0 for symbol in ("A", "B", "C", "D", "SOLO")}

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="failing"),
                OrderIntent(action="short", symbol="B", quantity=1.0, group_id="failing"),
                OrderIntent(action="long", symbol="C", quantity=1.0, group_id="ok"),
                OrderIntent(action="short", symbol="D", quantity=1.0, group_id="ok"),
                OrderIntent(action="long", symbol="SOLO", quantity=1.0),
            ],
            {
                symbol: {"close": 100.0, "volume": 10_000.0}
                for symbol in ("A", "B", "C", "D", "SOLO")
            },
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert "A" not in runner._positions
        assert "B" not in runner._positions
        assert runner._positions["C"].quantity == pytest.approx(1.0)
        assert runner._positions["D"].quantity == pytest.approx(1.0)
        assert runner._positions["SOLO"].quantity == pytest.approx(1.0)

    def test_live_group_adapter_preflight_failure_submits_no_sibling(self):
        adapter = _mock_order_adapter()

        def prepare_order(signal):
            if signal["canonical_symbol"] == "B":
                raise ValueError("below minimum notional")
            return signal

        adapter.prepare_order.side_effect = prepare_order
        adapter.place_order.side_effect = [
            _broker_report(order_id="c-1", quantity=1.0, average=100.0),
            _broker_report(order_id="d-1", quantity=1.0, average=100.0),
            _broker_report(order_id="solo-1", quantity=1.0, average=100.0),
        ]
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "B", "C", "D", "SOLO"]),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {symbol: 100.0 for symbol in ("A", "B", "C", "D", "SOLO")}

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="bad"),
                OrderIntent(action="short", symbol="B", quantity=1.0, group_id="bad"),
                OrderIntent(action="long", symbol="C", quantity=1.0, group_id="good"),
                OrderIntent(action="short", symbol="D", quantity=1.0, group_id="good"),
                OrderIntent(action="long", symbol="SOLO", quantity=1.0),
            ],
            {
                symbol: {"close": 100.0, "volume": 10_000.0}
                for symbol in ("A", "B", "C", "D", "SOLO")
            },
            TEST_CLOCK_NOW,
        )

        submitted_symbols = [
            call.args[0]["canonical_symbol"] for call in adapter.place_order.call_args_list
        ]
        assert complete is True
        assert submitted_symbols == ["C", "D", "SOLO"]
        assert set(runner._positions) == {"C", "D", "SOLO"}
        assert runner._halted is False
        assert len(runtime_events) == 1
        assert runtime_events[0].detail["reason"] == "group_preflight_rejected"
        assert runtime_events[0].detail["group_id"] == "bad"

    def test_live_group_shared_minimum_notional_submits_no_sibling(self):
        adapter = _mock_order_adapter()
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                symbols=["A", "B"],
                instrument_overrides={"B": {"min_notional": 150.0}},
            ),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {"A": 100.0, "B": 100.0}

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="pair"),
                OrderIntent(action="short", symbol="B", quantity=1.0, group_id="pair"),
            ],
            {
                "A": {"close": 100.0, "volume": 10_000.0},
                "B": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert runner._positions == {}
        adapter.place_order.assert_not_called()
        assert runtime_events[0].detail["reason"] == "group_preflight_rejected"
        assert "notional_below_minimum" in runtime_events[0].detail["message"]

    def test_live_group_rejects_asymmetric_adapter_quantity_rounding(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            **signal,
            "quantity": (
                signal["quantity"] / 2 if signal["canonical_symbol"] == "B" else signal["quantity"]
            ),
        }
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "B"]),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {"A": 100.0, "B": 100.0}

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="ratio"),
                OrderIntent(action="short", symbol="B", quantity=2.0, group_id="ratio"),
            ],
            {
                "A": {"close": 100.0, "volume": 10_000.0},
                "B": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert runner._active_orders == []
        assert runner._positions == {}
        adapter.place_order.assert_not_called()
        assert "relative leg ratios" in runtime_events[0].detail["message"]

    def test_live_group_rejects_equal_adapter_upsize_before_the_risk_replay(self):
        """An equal upsize keeps the leg ratio, so the ratio guard passes; the
        size guard still rejects it, and only that group -- the ungrouped
        sibling the adapter leaves alone is submitted as requested."""
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            **signal,
            "quantity": signal["quantity"] * (2 if signal["canonical_symbol"] != "SOLO" else 1),
        }
        adapter.place_order.return_value = _broker_report(
            order_id="solo-1", quantity=0.5, average=100.0
        )
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "B", "SOLO"]),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {"A": 100.0, "B": 100.0, "SOLO": 100.0}
        runner._risk_policy = RiskPolicy(max_order_notional=150.0)

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="oversized"),
                OrderIntent(action="short", symbol="B", quantity=1.0, group_id="oversized"),
                OrderIntent(action="long", symbol="SOLO", quantity=0.5),
            ],
            {symbol: {"close": 100.0, "volume": 10_000.0} for symbol in ("A", "B", "SOLO")},
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert set(runner._positions) == {"SOLO"}
        submitted = adapter.place_order.call_args.args[0]
        assert submitted["canonical_symbol"] == "SOLO"
        assert submitted["quantity"] == pytest.approx(0.5)
        assert "cannot increase quantity" in runtime_events[0].detail["message"]

    @pytest.mark.parametrize(
        ("failure", "expected_message"),
        [
            ("cash", "insufficient_cash"),
            ("tradability", "not tradable"),
            ("risk", "net exposure"),
        ],
    )
    def test_live_group_local_preflight_failure_isolated_from_ungrouped(
        self,
        failure,
        expected_message,
    ):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            order_id="solo-1", quantity=1.0, average=100.0
        )
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "B", "SOLO"]),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {"A": 100.0, "B": 100.0, "SOLO": 100.0}
        quantities = 600.0 if failure in ("cash", "risk") else 1.0
        if failure == "risk":
            runner._risk_policy = RiskPolicy(max_net_exposure=0.5)
        bars = {
            "A": {"close": 100.0, "volume": 10_000.0},
            "B": {
                "close": 100.0,
                "volume": 10_000.0,
                "can_buy": True,
                "can_sell": failure != "tradability",
            },
            "SOLO": {"close": 100.0, "volume": 10_000.0},
        }

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=quantities, group_id="rejected"),
                OrderIntent(action="short", symbol="B", quantity=quantities, group_id="rejected"),
                OrderIntent(action="long", symbol="SOLO", quantity=1.0),
            ],
            bars,
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert set(runner._positions) == {"SOLO"}
        submitted = adapter.place_order.call_args.args[0]
        assert submitted["canonical_symbol"] == "SOLO"
        assert expected_message in runtime_events[0].detail["message"]

    @pytest.mark.parametrize("quantity", [None, 1.0])
    def test_ungrouped_close_of_a_flat_symbol_is_a_no_op(self, quantity):
        """Matches simulated execution, where a close for a symbol with no
        position is skipped. Reachable through an idempotent close, a pending
        close whose position closed in the meantime, or restart drift; only a
        grouped close, which asks for fill-or-kill, treats it as a failure."""
        adapter = _mock_order_adapter()
        runner = self._make_trader(_HoldStrategy(), adapter)
        runner._last_prices = {"BTCUSDT": 100.0}

        complete = runner._execute_live_decision(
            [OrderIntent(action="close", symbol="BTCUSDT", quantity=quantity)],
            {"BTCUSDT": {"close": 100.0, "volume": 10_000.0}},
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        adapter.place_order.assert_not_called()

    def test_grouped_close_of_a_flat_symbol_rejects_its_group_only(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            order_id="solo-1", quantity=1.0, average=100.0
        )
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["A", "SOLO"]),
            on_runtime_event=runtime_events.append,
        )
        runner._last_prices = {"A": 100.0, "SOLO": 100.0}
        bars = {
            "A": {"close": 100.0, "volume": 10_000.0},
            "SOLO": {"close": 100.0, "volume": 10_000.0},
        }

        complete = runner._execute_live_decision(
            [
                OrderIntent(action="close", symbol="A", quantity=1.0, group_id="exit"),
                OrderIntent(action="long", symbol="SOLO", quantity=1.0),
            ],
            bars,
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        assert set(runner._positions) == {"SOLO"}
        assert "no open position" in runtime_events[0].detail["message"]

    def test_quantity_less_grouped_closes_match_validation_sim_and_live(self):
        ts = datetime(2025, 1, 2, tzinfo=UTC)
        bars = {symbol: {"close": 100.0, "volume": 10_000.0} for symbol in ("LONG", "SHORT")}
        decision = [
            OrderIntent(action="close", symbol="LONG", group_id="spread"),
            OrderIntent(action="close", symbol="SHORT", group_id="spread"),
        ]

        def positions() -> dict[str, PositionState]:
            return {
                "LONG": PositionState(
                    symbol="LONG",
                    side="long",
                    entry_price=100.0,
                    quantity=2.0,
                    entry_at=ts - timedelta(days=1),
                    periods_held=1,
                    entry_commission=0.0,
                    entry_slippage=0.0,
                    entry_tax=0.0,
                    total_entry_cost=200.0,
                    group_id="spread",
                ),
                "SHORT": PositionState(
                    symbol="SHORT",
                    side="short",
                    entry_price=100.0,
                    quantity=3.0,
                    entry_at=ts - timedelta(days=1),
                    periods_held=1,
                    entry_commission=0.0,
                    entry_slippage=0.0,
                    entry_tax=0.0,
                    total_entry_cost=300.0,
                    group_id="spread",
                ),
            }

        validate_strategy_decision(
            decision,
            {"LONG", "SHORT"},
            primary_symbol="LONG",
            bars=bars,
            positions=positions(),
        )
        simulated_positions = positions()
        simulated = execute_order_intents(
            decision,
            simulated_positions,
            100_000.0,
            ts,
            get_price=lambda _symbol, _intent: 100.0,
            get_cost_model=lambda _symbol: CostModel.zero(),
            primary_symbol="LONG",
            atomic_groups=True,
        )

        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = lambda signal: _broker_report(
            order_id=signal["canonical_symbol"],
            quantity=signal["quantity"],
            average=100.0,
            executed_at=ts,
        )
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["LONG", "SHORT"]),
        )
        runner._positions = positions()
        runner._last_prices = {"LONG": 100.0, "SHORT": 100.0}

        complete = runner._execute_live_decision(decision, bars, ts)

        assert simulated_positions == {}
        assert [event.fill_quantity for event in simulated.events] == [2.0, 3.0]
        assert complete is True
        assert runner._halted is False
        assert runner._positions == {}
        assert [call.args[0]["quantity"] for call in adapter.place_order.call_args_list] == [
            2.0,
            3.0,
        ]

    @pytest.mark.parametrize(
        ("action", "blocked_field", "expected_side"),
        [("long", "can_buy", "buy"), ("short", "can_sell", "sell")],
    )
    def test_unavailable_ungrouped_side_is_audited_and_skipped(
        self,
        action,
        blocked_field,
        expected_side,
    ):
        adapter = _mock_order_adapter()
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            on_runtime_event=runtime_events.append,
        )
        bar = {
            "close": 100.0,
            "volume": 10_000.0,
            "can_buy": True,
            "can_sell": True,
        }
        bar[blocked_field] = False

        complete = runner._execute_live_decision(
            [OrderIntent(action=action, symbol="BTCUSDT", quantity=1.0)],
            {"BTCUSDT": bar},
            TEST_CLOCK_NOW,
        )

        assert complete is True
        assert runner._halted is False
        adapter.place_order.assert_not_called()
        assert runtime_events[-1].event_type == "decision_skipped"
        assert runtime_events[-1].symbol == "BTCUSDT"
        assert runtime_events[-1].detail == {
            "reason": "side_not_tradable",
            "side": expected_side,
        }

    def test_live_group_checkpoints_all_siblings_and_resumes_after_restart(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            state_store=store,
            config=_test_cfg(mode="live", symbols=["A", "B"]),
        )
        runner._last_prices = {"A": 100.0, "B": 100.0}
        checkpointed_before_submit: list[str] = []

        def place_order(signal):
            if not checkpointed_before_submit:
                assert adapter.prepare_order.call_count == 2
                checkpoint = store.load(runner._state_key)
                assert checkpoint is not None
                checkpointed_before_submit.extend(
                    tracked.request.symbol for tracked in checkpoint.active_orders
                )
            if signal["canonical_symbol"] == "A":
                return _broker_report(order_id="a-1", quantity=1.0, average=100.0)
            return _broker_report(order_id="b-1", status="accepted", quantity=1.0, filled=0.0)

        adapter.place_order.side_effect = place_order
        complete = runner._execute_live_decision(
            [
                OrderIntent(action="long", symbol="A", quantity=1.0, group_id="pair"),
                OrderIntent(action="long", symbol="B", quantity=1.0, group_id="pair"),
            ],
            {
                "A": {"close": 100.0, "volume": 10_000.0},
                "B": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is False
        assert checkpointed_before_submit == ["A", "B"]
        assert runner._positions["A"].quantity == pytest.approx(1.0)
        assert [tracked.request.symbol for tracked in runner._active_orders] == ["B"]

        adapter.get_order.return_value = _broker_report(order_id="b-1", quantity=1.0, average=100.0)
        adapter.get_position.side_effect = lambda request: {
            "symbol": request.venue_symbol,
            "size": 1.0,
            "avg_price": 100.0,
            "unrealized_pnl": 0.0,
        }
        restored = self._make_trader(
            _HoldStrategy(),
            adapter,
            state_store=store,
            config=_test_cfg(mode="live", symbols=["A", "B"]),
        )
        restored._initialize_run()

        assert restored._halted is False
        assert restored._active_orders == []
        assert set(restored._positions) == {"A", "B"}
        assert adapter.place_order.call_count == 2

    def test_live_group_ambiguous_placement_still_halts_whole_account(self):
        """A confirmed broker rejection scopes to its group (see above), but
        an *ambiguous* failure (submit and find_order both fail) means the
        order's true broker-side state is unknown — that must still halt the
        whole account even for a grouped leg, since the order may actually be
        live at the venue and dropping it from tracking would lose it."""
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = RuntimeError("connection refused")
        adapter.find_order.return_value = None
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", symbols=["SPOT", "PERP"]),
        )
        alerts = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner._last_prices = {"SPOT": 100.0, "PERP": 100.0}

        complete = runner._execute_live_decision(
            [
                OrderIntent(
                    action="long", symbol="SPOT", quantity=1.0, reason="basis", group_id="basis"
                ),
                OrderIntent(
                    action="short", symbol="PERP", quantity=1.0, reason="basis", group_id="basis"
                ),
            ],
            {
                "SPOT": {"close": 100.0, "volume": 10_000.0},
                "PERP": {"close": 100.0, "volume": 10_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert complete is False
        assert runner._halted is True
        assert any(
            m == "send_alert" and "Ambiguous Order Placement" in kw["title"] for m, kw in alerts
        )

    def test_post_fill_risk_uses_broker_price_and_halts(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report(
            order_id="slipped",
            quantity=100.0,
            average=1_000.0,
            executed_at=TEST_CLOCK_NOW,
        )

        class Buy(Strategy):
            def on_bar(self, ctx):
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=100.0)]

        runner = self._make_trader(
            Buy(),
            adapter,
            config=_test_cfg(
                mode="live",
                risk=RiskPolicy(max_position_weight=0.5),
            ),
        )
        runner.run(max_iterations=1)

        assert runner._halted is True

    def test_periodic_position_mismatch_halts(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(mode="live", reconciliation_interval_seconds=1),
        )
        runner._last_reconciliation_at = TEST_CLOCK_NOW - timedelta(seconds=2)
        adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 1.0,
            "avg_price": 100.0,
        }

        runner._maybe_reconcile_runtime()

        assert runner._halted is True

    def test_run_releases_lease_when_startup_initialization_raises(self):
        store = MemoryLiveStateStore()
        runner = self._make_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        runner._reconcile_positions = MagicMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            runner.run(max_iterations=1)

        assert store.acquire_lease(runner._state_key) is True
        assert store.acquire_lease(runner._account_lease_key) is True

    def test_state_lease_conflict_releases_account_ownership(self):
        store = MemoryLiveStateStore()
        runner = self._make_trader(_HoldStrategy(), _mock_order_adapter(), state_store=store)
        assert store.acquire_lease(runner._state_key) is True

        with pytest.raises(RuntimeError, match="owns state_key"):
            runner.run(max_iterations=1)

        assert store.acquire_lease(runner._account_lease_key) is True

    def test_three_live_deployments_isolate_accounts_across_strategy_configs(self):
        store = MemoryLiveStateStore()
        shared_account = AccountConfig(
            account_id="shared-account",
            currency="USDT",
            initial_cash=100_000.0,
        )
        first = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            config=_test_cfg(mode="live", strategy_name="first", account=shared_account),
        )
        second_adapter = _mock_order_adapter()
        second = self._make_trader(
            _HoldStrategy(),
            second_adapter,
            state_store=store,
            config=_test_cfg(mode="live", strategy_name="second", account=shared_account),
        )
        third = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            config=_test_cfg(
                mode="live",
                strategy_name="third",
                account=AccountConfig(
                    account_id="independent-account",
                    currency="USDT",
                    initial_cash=100_000.0,
                ),
            ),
        )

        first._initialize_run()
        third._initialize_run()
        try:
            with pytest.raises(RuntimeError, match="owns account_id='shared-account'"):
                second.run(max_iterations=1)
        finally:
            first._release_lease()
            third._release_lease()

        second_adapter.get_position.assert_not_called()
        second_adapter.get_balance.assert_not_called()
        assert first._state_key != second._state_key

    def test_ready_callback_runs_after_ownership_and_reconciliation(self):
        store = MemoryLiveStateStore()
        observations: list[tuple[str, bool, bool]] = []
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            on_ready=lambda run_id: observations.append(
                (
                    run_id,
                    runner._account_lease_acquired,
                    runner._last_reconciliation_at is not None,
                )
            ),
        )

        runner.run(max_iterations=1)

        assert observations == [(runner.run_id, True, True)]

    def test_ready_publication_failure_releases_live_ownership(self):
        store = MemoryLiveStateStore()
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
            on_ready=MagicMock(side_effect=OSError("read-only readiness path")),
        )

        with pytest.raises(OSError, match="read-only readiness path"):
            runner.run(max_iterations=1)

        assert store.acquire_lease(runner._account_lease_key) is True
        assert store.acquire_lease(runner._state_key) is True

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

    def test_live_planning_uses_execution_time_equity_after_other_holding_gaps(self):
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                risk=RiskPolicy(max_position_weight=0.5),
            ),
        )
        runner._cash = 5_000.0
        runner._prev_equity = 10_000.0
        runner._positions["AAA"] = PositionState(
            symbol="AAA",
            side="long",
            entry_price=100.0,
            quantity=50.0,
            entry_at=TEST_CLOCK_NOW,
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=5_000.0,
        )

        requests = runner._plan_live_orders(
            [OrderIntent(action="long", symbol="BBB", quantity=100.0)],
            {
                "AAA": {"close": 20.0, "volume": 1_000.0},
                "BBB": {"close": 100.0, "volume": 1_000.0},
            },
            TEST_CLOCK_NOW,
        )

        assert [request.quantity for request in requests] == [30.0]

    def test_live_planning_applies_instrument_quantity_contract_before_adapter(self):
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            config=_test_cfg(
                mode="live",
                symbols=["COIN"],
                instrument_overrides={
                    "COIN": {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "quantity_step": 0.1,
                        "min_quantity": 0.2,
                    }
                },
                symbol_cost_overrides={"COIN": {"multiplier": 1.0}},
            ),
        )

        requests = runner._plan_live_orders(
            [OrderIntent(action="long", symbol="COIN", quantity=1.29)],
            {"COIN": {"close": 100.0, "volume": 1_000.0}},
            TEST_CLOCK_NOW,
        )

        assert [request.quantity for request in requests] == [pytest.approx(1.2)]

    def test_live_group_rejects_shared_normalization_that_changes_leg_ratios(self):
        runtime_events = []
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                instrument_overrides={
                    "AAA": {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "quantity_step": 1.0,
                    },
                    "BBB": {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "quantity_step": 2.0,
                    },
                },
            ),
            on_runtime_event=runtime_events.append,
        )

        requests = runner._plan_live_orders(
            [
                OrderIntent(action="long", symbol="AAA", quantity=2.7, group_id="spread"),
                OrderIntent(action="short", symbol="BBB", quantity=5.0, group_id="spread"),
            ],
            {symbol: {"close": 100.0, "volume": 1_000.0} for symbol in ("AAA", "BBB")},
            TEST_CLOCK_NOW,
        )

        assert requests == []
        assert runtime_events[0].detail["reason"] == "group_preflight_rejected"
        assert "changes relative leg ratios" in runtime_events[0].detail["message"]

    def test_adapter_prepared_quantity_must_still_match_shared_increment(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {**signal, "quantity": 1.5}
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                symbols=["COIN"],
                instrument_overrides={
                    "COIN": {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "quantity_step": 1.0,
                        "min_quantity": 1.0,
                    }
                },
            ),
        )

        with pytest.raises(ValueError, match="post-adapter risk validation"):
            runner._plan_live_orders(
                [OrderIntent(action="long", symbol="COIN", quantity=2.0)],
                {"COIN": {"close": 100.0, "volume": 1_000.0}},
                TEST_CLOCK_NOW,
            )

    def test_live_order_batch_rejects_gross_exposure_before_submission(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                risk=RiskPolicy(max_gross_exposure=0.5),
            ),
        )
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        with pytest.raises(ValueError, match="post-decision gross exposure"):
            runner._plan_live_orders(
                [OrderIntent(action="long", symbol="BTCUSDT", quantity=600.0)],
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

        adapter.place_order.assert_not_called()

    def test_live_limit_order_exposure_uses_limit_price(self):
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            config=_test_cfg(
                mode="live",
                risk=RiskPolicy(max_gross_exposure=0.5),
            ),
        )

        with pytest.raises(ValueError, match="post-decision gross exposure"):
            runner._plan_live_orders(
                [
                    OrderIntent(
                        action="long",
                        symbol="BTCUSDT",
                        quantity=500.0,
                        limit_price=120.0,
                    )
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
                datetime(2025, 1, 1, tzinfo=UTC),
            )

    def test_live_order_batch_rejects_transient_net_exposure_before_submission(self):
        adapter = _mock_order_adapter()
        runner = self._make_trader(
            _HoldStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                symbols=["AAA", "BBB"],
                risk=RiskPolicy(max_net_exposure=0.5),
            ),
        )
        ts = datetime(2025, 1, 1, tzinfo=UTC)

        with pytest.raises(ValueError, match="post-decision absolute net exposure"):
            runner._plan_live_orders(
                [
                    OrderIntent(action="long", symbol="AAA", quantity=600.0),
                    OrderIntent(action="short", symbol="BBB", quantity=600.0),
                ],
                {
                    symbol: {
                        "open": 100.0,
                        "high": 100.0,
                        "low": 100.0,
                        "close": 100.0,
                        "volume": 1_000.0,
                    }
                    for symbol in ("AAA", "BBB")
                },
                ts,
            )

        adapter.place_order.assert_not_called()

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
        fetcher.fetch_ohlcv = None
        runner = LiveTrader(
            strategy,
            _simple_feature_fn,
            config=_test_cfg(mode="live"),
            adapter=fetcher,
            cost_model=_zero_cost_model(),
            order_adapter=adapter,
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            state_store=MemoryLiveStateStore(),
            runtime_revision="test-runtime",
            clock=lambda: TEST_CLOCK_NOW,
        )
        runner.STALE_DATA_TOLERANCE_BARS = 100

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
                execution=ExecutionPolicy(
                    live_order_timeout_seconds=30,
                    warmup_periods=5,
                ),
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
                execution=ExecutionPolicy(
                    live_order_timeout_seconds=30,
                    warmup_periods=5,
                ),
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

    def test_timeout_cancels_when_status_lookup_raises(self):
        adapter = _mock_order_adapter()
        accepted = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.place_order.return_value = accepted
        adapter.get_order.side_effect = ConnectionError("broker status unavailable")
        adapter.cancel_order.return_value = {
            "id": "open-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        now = [TEST_CLOCK_NOW]
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(
                    live_order_timeout_seconds=30,
                    warmup_periods=5,
                ),
            ),
            clock=lambda: now[0],
        )
        runner.run(max_iterations=1)

        now[0] += timedelta(seconds=30)
        runner._poll_cycle()

        adapter.cancel_order.assert_called_once_with("open-1", "BTC/USDT")
        assert runner._halted is True
        assert runner._active_orders == []

    def test_timeout_cancels_when_filled_status_report_is_incomplete(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.get_order.return_value = {
            "id": "open-1",
            "status": "filled",
            "amount": 1.0,
            "filled": 1.0,
        }
        adapter.cancel_order.return_value = {
            "id": "open-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        now = [TEST_CLOCK_NOW]
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(
                    live_order_timeout_seconds=30,
                    warmup_periods=5,
                ),
            ),
            clock=lambda: now[0],
        )
        runner.run(max_iterations=1)

        now[0] += timedelta(seconds=30)
        runner._poll_cycle()

        adapter.cancel_order.assert_called_once_with("open-1", "BTC/USDT")
        assert runner._halted is True
        assert runner._active_orders == []
        assert runner._positions == {}

    def test_halt_persists_failed_cancel_and_retries_after_restart(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        accepted = {
            "id": "open-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        cancelled = {
            "id": "open-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.place_order.return_value = accepted
        adapter.get_order.return_value = accepted
        adapter.cancel_order.side_effect = [
            ConnectionError("cancel endpoint unavailable"),
            cancelled,
        ]
        first = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        first.run(max_iterations=1)

        first.halt("operator requested halt")

        assert first._halted is True
        assert first._active_orders[0].cancel_requested is True
        assert next(iter(store.orders.values())).cancel_requested is True

        second = self._make_trader(_AlwaysBuyStrategy(), adapter, state_store=store)
        second.run(max_iterations=1)

        assert second._halted is True
        assert second._active_orders == []
        assert adapter.cancel_order.call_count == 2
        adapter.place_order.assert_called_once()

    def test_timeout_without_order_id_keeps_client_lookup_until_cancelled(self):
        adapter = _mock_order_adapter()
        adapter.place_order.side_effect = ConnectionError("placement response lost")
        adapter.find_order.side_effect = [
            ConnectionError("initial lookup unavailable"),
            ConnectionError("timeout lookup unavailable"),
            None,
        ]
        now = [TEST_CLOCK_NOW]
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                execution=ExecutionPolicy(
                    live_order_timeout_seconds=30,
                    warmup_periods=5,
                ),
            ),
            clock=lambda: now[0],
        )
        runner.run(max_iterations=1)

        now[0] += timedelta(seconds=30)
        runner._poll_cycle()

        assert runner._halted is True
        assert runner._active_orders[0].order_id == ""
        assert runner._active_orders[0].cancel_requested is True

        adapter.find_order.side_effect = None
        adapter.find_order.return_value = {
            "id": "recovered-1",
            "status": "accepted",
            "amount": 1.0,
            "filled": 0.0,
        }
        adapter.cancel_order.return_value = {
            "id": "recovered-1",
            "status": "cancelled",
            "amount": 1.0,
            "filled": 0.0,
        }
        runner._poll_cycle()

        adapter.cancel_order.assert_called_once_with("recovered-1", "BTC/USDT")
        adapter.place_order.assert_called_once()
        assert runner._active_orders == []

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
            execution=ExecutionPolicy(
                live_order_timeout_seconds=30,
                warmup_periods=5,
            ),
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

        first._flatten_account_and_halt(
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

    def test_restore_reports_state_recovered_runtime_event(self):
        store = MemoryLiveStateStore()
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = _broker_report()

        first = self._make_trader(_HoldStrategy(), adapter, state_store=store)
        first.run(max_iterations=1)

        on_runtime_event = MagicMock()
        adapter.get_position.return_value = {
            "symbol": "BTCUSDT",
            "size": 0.0,
            "avg_price": 0.0,
            "unrealized_pnl": 0.0,
        }
        self._make_trader(
            _HoldStrategy(), adapter, state_store=store, on_runtime_event=on_runtime_event
        )

        on_runtime_event.assert_called_once()
        event = on_runtime_event.call_args[0][0]
        assert event.event_type == "state_recovered"

    def test_restored_position_without_broker_cost_basis_reconciles_on_size_alone(self):
        """CCXT spot balances never carry avg_price; restore must not halt
        forever just because that field is absent — only size/side are
        cross-checked against the broker in that case."""
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

    def test_halt_reset_starts_new_risk_epoch(self):
        store = MemoryLiveStateStore()
        runner = self._make_trader(
            _HoldStrategy(),
            _mock_order_adapter(),
            state_store=store,
        )
        runner._halted = True
        runner._equity_peak = 120_000.0
        runner._prev_equity = 80_000.0
        runner._cash = 90_000.0

        runner.reset_halt()

        assert runner._halted is False
        assert runner._equity_peak == pytest.approx(90_000.0)
        assert runner._prev_equity == pytest.approx(90_000.0)
        restored = store.load(runner._state_key)
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

    def test_orphan_check_recognizes_compact_broker_client_ids(self):
        class CompactIdAdapter(MagicMock):
            def broker_client_order_id(self, client_order_id: str) -> str:
                return client_order_id[:6]

        adapter = _mock_order_adapter(CompactIdAdapter)
        request = OrderRequest(
            client_order_id="client-1-long-canonical-id",
            symbol="BTCUSDT",
            side="buy",
            quantity=1.0,
            order_type="market",
            submitted_at=TEST_CLOCK_NOW,
        )
        adapter.list_open_orders.return_value = [
            {"id": "", "clientOrderId": adapter.broker_client_order_id(request.client_order_id)}
        ]
        runner = self._make_trader(MagicMock(spec=Strategy), adapter)
        runner._active_orders = [TrackedOrder(request=request, placement_attempted=True)]

        runner._reconcile_open_orders()

        assert runner._halted is False

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
        events: list[PositionEvent] = []
        store = MemoryLiveStateStore()

        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            state_store=store,
        )
        runner._on_position_event = lambda event, _sequence: events.append(event)
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
        assert events[-1].realized_pnl == 7.0
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
                        limit_price=99.5,
                    )
                ]

        runner = self._make_trader(LimitBuy(), adapter)
        runner.run(max_iterations=1)

        signal = adapter.place_order.call_args.args[0]
        assert signal["order_type"] == "limit"
        assert signal["price"] == 99.5
        assert runner._positions == {}

    def test_invalid_price_grid_halts_before_adapter_preparation(self):
        adapter = _mock_order_adapter()

        class HalfTickLimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        limit_price=99.625,
                    )
                ]

        runner = self._make_trader(
            HalfTickLimitBuy(),
            adapter,
            config=_test_cfg(
                mode="live",
                instrument_overrides={"BTCUSDT": {"price_increment": 0.25}},
            ),
        )

        runner.run(max_iterations=1)

        assert runner._halted is True
        adapter.prepare_order.assert_not_called()
        adapter.place_order.assert_not_called()

    def test_minimum_notional_is_skipped_before_adapter_preparation(self):
        adapter = _mock_order_adapter()
        runtime_events = []
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="live",
                instrument_overrides={"BTCUSDT": {"min_notional": 200.0}},
            ),
            on_runtime_event=runtime_events.append,
        )

        runner.run(max_iterations=1)

        assert runner._halted is False
        assert any(
            event.detail.get("reason") == "notional_below_minimum" for event in runtime_events
        )
        adapter.prepare_order.assert_not_called()
        adapter.place_order.assert_not_called()

    def test_simulation_uses_the_same_minimum_notional_contract(self):
        adapter = _mock_order_adapter()
        runtime_events = []
        runner = self._make_trader(
            _AlwaysBuyStrategy(),
            adapter,
            config=_test_cfg(
                mode="sim",
                instrument_overrides={"BTCUSDT": {"min_notional": 200.0}},
            ),
            on_runtime_event=runtime_events.append,
        )
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
        assert any(
            event.detail.get("reason") == "notional_below_minimum" for event in runtime_events
        )
        adapter.prepare_order.assert_not_called()

    def test_limit_price_collar_halts_before_submission(self):
        adapter = _mock_order_adapter()

        class FatFingerLimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        limit_price=50.0,
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

    @pytest.mark.parametrize(
        ("risk_policy", "requested_quantity", "prepared_quantity", "message"),
        [
            # WHY: the size check runs before the risk replay and does not
            # depend on any limit being set, so every RiskPolicy -- including
            # the all-None default -- rejects an enlargement the same way.
            (RiskPolicy(), 1.0, 2.0, "cannot increase quantity"),
            (RiskPolicy(max_order_notional=150.0), 1.0, 2.0, "cannot increase quantity"),
            (RiskPolicy(max_position_weight=0.5), 400.0, 600.0, "cannot increase quantity"),
            (RiskPolicy(max_gross_exposure=0.5), 400.0, 600.0, "cannot increase quantity"),
        ],
    )
    def test_enlarged_prepared_quantity_is_rejected_before_the_risk_replay(
        self,
        risk_policy,
        requested_quantity,
        prepared_quantity,
        message,
    ):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            **signal,
            "quantity": prepared_quantity,
        }

        class Buy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=requested_quantity,
                    )
                ]

        runner = self._make_trader(Buy(), adapter)
        runner._risk_policy = risk_policy
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()
        assert any(message in kwargs.get("message", "") for _, kwargs in alerts)

    def test_downward_lot_rounding_is_submitted_at_the_rounded_size(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {**signal, "quantity": 0.9}
        adapter.place_order.return_value = {
            "id": "market-1",
            "status": "submitted",
            "amount": 0.9,
            "filled": 0.0,
        }

        class Buy(Strategy):
            def on_bar(self, ctx):
                return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]

        runner = self._make_trader(Buy(), adapter)
        runner._risk_policy = RiskPolicy()

        runner.run(max_iterations=1)

        assert runner._halted is False
        adapter.place_order.assert_called_once()
        assert adapter.place_order.call_args[0][0]["quantity"] == pytest.approx(0.9)

    def test_prepared_limit_price_change_halts_before_submission(self):
        adapter = _mock_order_adapter()
        adapter.prepare_order.side_effect = lambda signal: {
            **signal,
            "price": 200.0,
        }

        class LimitBuy(Strategy):
            def on_bar(self, ctx):
                return [
                    OrderIntent(
                        action="long",
                        symbol=ctx.symbol,
                        quantity=1.0,
                        limit_price=100.0,
                    )
                ]

        runner = self._make_trader(LimitBuy(), adapter)
        runner._risk_policy = RiskPolicy()

        runner.run(max_iterations=1)

        assert runner._halted is True
        assert runner._active_orders == []
        adapter.place_order.assert_not_called()

    def test_post_adapter_replay_preserves_group_id_for_scale_in(self):
        runner = self._make_trader(_HoldStrategy(), _mock_order_adapter())
        runner._positions["BTCUSDT"] = PositionState(
            symbol="BTCUSDT",
            side="long",
            entry_price=100.0,
            quantity=1.0,
            entry_at=TEST_CLOCK_NOW,
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=100.0,
            group_id="pair",
        )

        with patch(
            "librae.live.engine.execute_order_intents",
            wraps=execute_order_intents,
        ) as execute:
            requests = runner._plan_live_orders(
                [
                    OrderIntent(
                        action="long",
                        symbol="BTCUSDT",
                        quantity=1.0,
                        group_id="pair",
                    )
                ],
                {
                    "BTCUSDT": {
                        "close": 100.0,
                        "volume": 1_000.0,
                    }
                },
                TEST_CLOCK_NOW,
            )

        assert requests[0].group_id == "pair"
        assert [call.args[0][0].group_id for call in execute.call_args_list] == ["pair", "pair"]

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
                        limit_price=99.5,
                    )
                ]

        runner = self._make_trader(LimitBuy(), adapter, state_store=store)
        runner._risk_policy = RiskPolicy(max_order_notional=200.0)
        runner.run(max_iterations=1)

        signal = adapter.place_order.call_args.args[0]
        tracked = next(iter(store.orders.values()))
        assert signal["quantity"] == 1.0
        assert signal["price"] == 99.5
        assert tracked.request.quantity == 1.0
        assert tracked.request.limit_price == 99.5

    def test_identity_preparation_still_submits_under_risk_limits(self):
        adapter = _mock_order_adapter()
        adapter.place_order.return_value = {
            "id": "market-1",
            "status": "submitted",
            "amount": 1.0,
            "filled": 0.0,
        }
        runner = self._make_trader(_AlwaysBuyStrategy(), adapter)
        runner._risk_policy = RiskPolicy(
            max_order_notional=150.0,
            max_position_weight=0.01,
            max_gross_exposure=0.01,
        )

        runner.run(max_iterations=1)

        assert runner._halted is False
        signal = adapter.place_order.call_args.args[0]
        assert signal["quantity"] == 1.0
        assert signal["order_type"] == "market"

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

    def test_simulation_only_protection_fails_closed(self):
        adapter = _mock_order_adapter()
        action = OrderIntent(
            action="long",
            symbol="BTCUSDT",
            quantity=1.0,
            stop_price=95.0,
        )

        class InvalidIntent(Strategy):
            def on_bar(self, ctx):
                return [action]

        runner = self._make_trader(InvalidIntent(), adapter)
        alerts: list[tuple[str, dict]] = []
        runner._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        runner.run(max_iterations=1)

        assert runner._halted is True
        assert adapter.place_order.call_count == 0
        assert any(
            "broker-native protective orders" in kwargs.get("message", "") for _, kwargs in alerts
        )


class TestCryptoLiveFactory:
    """LiveTrader without an explicit adapter= override (the real code path
    used in production) for crypto (non-tw_futures) live mode.

    _make_runner above always passes adapter=, which bypasses this branch
    entirely — so it never covered the auto-wiring that replaced the old
    unconditional NotImplementedError for crypto live mode."""

    def _build(self, monkeypatch):
        monkeypatch.setenv("BINANCE_API_KEY", "k")
        monkeypatch.setenv("BINANCE_API_SECRET", "s")
        with (
            patch("librae.brokers.crypto_adapter.CryptoAdapter") as mock_cls,
            patch(
                "librae.orchestration.live._build_state_store",
                return_value=MemoryLiveStateStore(),
            ),
            patch("librae.orchestration.live._build_notifier", return_value=None),
            patch("librae.orchestration.live._TimescaleCallbacks"),
        ):
            mock_cls.return_value = MagicMock()
            trader = build_live_trader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(mode="live", broker="binance"),
                runtime_revision="test-runtime",
            )
        return trader, mock_cls

    def test_auto_builds_order_adapter_from_env(self, monkeypatch):
        trader, adapter_cls = self._build(monkeypatch)
        adapter_cls.assert_called_once()
        assert adapter_cls.call_args.kwargs["credentials"].exchange_id == "binance"
        assert trader._executor.get_order_adapter("BTCUSDT") is adapter_cls.return_value


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
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
            warmup_fetcher=None,
            state_store=MemoryLiveStateStore(),
        )

        assert trader._get_cost_model("BTCUSDT").commission_rate == 0.001
        assert trader._get_cost_model("MU").long_margin_rate == 1.0
        assert trader._get_cost_model("MU").short_margin_rate == 0.5


class TestIBKRLiveFactory:
    def test_us_equity_builds_ibkr_instead_of_crypto(self):
        with (
            patch("librae.brokers.ibkr_adapter.IBKRAdapter") as mock_cls,
            patch(
                "librae.orchestration.live._build_state_store",
                return_value=MemoryLiveStateStore(),
            ),
            patch("librae.orchestration.live._build_notifier", return_value=None),
            patch("librae.orchestration.live._TimescaleCallbacks"),
        ):
            mock_cls.return_value = _mock_order_adapter()
            trader = build_live_trader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=_test_cfg(
                    mode="live",
                    symbols=["MU"],
                    market="us_equity",
                    data_source="ibkr",
                    broker="ibkr",
                ),
                runtime_revision="test-runtime",
            )

        mock_cls.assert_called_once_with(trading_enabled=True)
        assert trader._executor.get_order_adapter("MU") is mock_cls.return_value


class TestShioajiLiveFactory:
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
        with (
            patch("librae.brokers.shioaji_adapter.ShioajiAdapter") as mock_cls,
            patch(
                "librae.orchestration.live._build_state_store",
                return_value=MemoryLiveStateStore(),
            ),
            patch("librae.orchestration.live._build_notifier", return_value=None),
            patch("librae.orchestration.live._TimescaleCallbacks"),
        ):
            mock_cls.return_value = MagicMock()
            trader = build_live_trader(
                _HoldStrategy(),
                _simple_feature_fn,
                config=self._shioaji_cfg(mode="live"),
                runtime_revision="test-runtime",
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

        with (
            patch("librae.brokers.shioaji_adapter.ShioajiAdapter") as mock_cls,
            patch(
                "librae.orchestration.live._build_state_store",
                return_value=MemoryLiveStateStore(),
            ),
            patch("librae.orchestration.live._build_notifier", return_value=None),
            patch("librae.orchestration.live._TimescaleCallbacks"),
        ):
            mock_shioaji = _mock_order_adapter()
            mock_shioaji.place_order.side_effect = lambda signal: _broker_report(
                quantity=signal["quantity"],
                average=104.25,
            )
            mock_shioaji.fetch_ohlcv.side_effect = (
                lambda symbol, tf, limit, drop_incomplete=False, calendar_id=None, continuous_alias=False, contract_month=None: (
                    fetcher()
                )
            )
            mock_cls.return_value = mock_shioaji

            trader = build_live_trader(
                _AlwaysBuyStrategy(),
                _simple_feature_fn,
                config=self._shioaji_cfg(mode="live"),
                runtime_revision="test-runtime",
            )
            trader._clock = lambda: TEST_CLOCK_NOW
            trader.STALE_DATA_TOLERANCE_BARS = 100
            trader._sleep = lambda _seconds: None  # no real delays in unit tests
            trader.run(max_iterations=2)

        # The completed-bar decision is submitted to Shioaji immediately.
        assert mock_shioaji.place_order.call_count >= 1
        first_call_signal = mock_shioaji.place_order.call_args_list[0].args[0]
        assert first_call_signal["side"] == "buy"
        assert first_call_signal["symbol"] == "TXFR1"
