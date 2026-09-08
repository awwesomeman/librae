"""Tests for DB-first warmup fetcher in LiveTrader."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pandas as pd
from librae.core.run_config import AccountConfig, ExecutionPolicy, RunConfig
from librae.live.state import MemoryLiveStateStore

from tests.conftest import make_test_cfg


def _test_cfg(**overrides) -> RunConfig:
    overrides.setdefault(
        "execution",
        ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=50,
        ),
    )
    return make_test_cfg(**overrides)


def _bars(timestamps: list[datetime], closes: list[float] | None = None) -> pd.DataFrame:
    prices = closes or [float(100 + offset) for offset in range(len(timestamps))]
    return pd.DataFrame(
        {
            "ts": pd.DatetimeIndex(timestamps),
            "open": prices,
            "high": [price + 1.0 for price in prices],
            "low": [price - 1.0 for price in prices],
            "close": prices,
            "volume": [100.0] * len(timestamps),
        }
    )


class TestWarmupFetcher:
    """LiveTrader warmup_fetcher uses get_ohlcv for initial load."""

    def test_warmup_from_db_skips_exchange_api(self):
        """When warmup_fetcher returns data, regular fetcher is not called."""
        from librae.live.engine import LiveTrader

        warmup_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC"),
                "open": range(1, 101),
                "high": range(1, 101),
                "low": range(1, 101),
                "close": range(1, 101),
                "volume": [100] * 100,
            }
        )
        mock_warmup = MagicMock(return_value=warmup_df)
        mock_fetcher = MagicMock()
        mock_strategy = MagicMock()

        cfg = _test_cfg()
        trader = LiveTrader(
            mock_strategy,
            lambda x: x,
            config=cfg,
            adapter=mock_fetcher,
            warmup_fetcher=mock_warmup,
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        assert result is not None
        assert len(result) == 50
        mock_warmup.assert_called_once_with("BTCUSDT", trader._timeframe, 51)
        mock_fetcher.assert_not_called()

    def test_warmup_fetcher_none_uses_regular_fetcher(self):
        """When warmup_fetcher is None, uses regular fetcher for warmup."""
        from librae.live.engine import LiveTrader

        mock_strategy = MagicMock()
        warmup_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
                "open": range(1, 11),
                "high": range(1, 11),
                "low": range(1, 11),
                "close": range(1, 11),
                "volume": [100] * 10,
            }
        )
        mock_fetcher = MagicMock(return_value=warmup_df)
        mock_fetcher.fetch_ohlcv = None

        cfg = _test_cfg(
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=10,
            )
        )
        trader = LiveTrader(
            mock_strategy,
            lambda x: x,
            config=cfg,
            adapter=mock_fetcher,
            warmup_fetcher=None,
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        mock_fetcher.assert_called_once()
        assert len(result) == 10

    def test_initial_request_allows_one_forming_bar_to_be_removed(self):
        from librae.live.engine import LiveTrader

        frame = _bars([datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(6)])
        requests: list[int] = []

        def fetcher(_symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append(limit)
            # Model an API whose limit includes the current forming bar, which
            # its adapter then removes before returning completed history.
            return frame.iloc[-limit:-1].reset_index(drop=True)

        trader = LiveTrader(
            MagicMock(),
            lambda history: history,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=fetcher,
            clock=lambda: datetime(2025, 1, 1, 6, tzinfo=UTC),
        )

        result = trader._fetch_with_cache("BTCUSDT")

        assert requests == [6]
        assert result is not None
        assert len(result) == 5

    def test_invalid_runtime_ohlcv_is_not_cached(self, caplog):
        from librae.live.engine import LiveTrader

        invalid_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [100.0, float("nan")],
            }
        )
        mock_fetcher = MagicMock(return_value=invalid_df)
        mock_fetcher.fetch_ohlcv = None
        trader = LiveTrader(
            MagicMock(),
            lambda x: x,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=2,
                )
            ),
            adapter=mock_fetcher,
            warmup_fetcher=None,
            on_bar=None,
            on_position_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        assert trader._fetch_with_cache("BTCUSDT") is None
        assert "BTCUSDT" not in trader._ohlcv_cache
        assert "runtime data OHLCV values must be finite" in caplog.text

    def test_regular_session_shortfall_across_weekend_and_holiday_is_backfilled(self):
        from librae.live.engine import LiveTrader

        timestamps = [
            datetime(2024, 12, 27, 21, tzinfo=UTC),
            datetime(2024, 12, 30, 21, tzinfo=UTC),
            datetime(2024, 12, 31, 21, tzinfo=UTC),
            datetime(2025, 1, 2, 21, tzinfo=UTC),
            datetime(2025, 1, 3, 21, tzinfo=UTC),
        ]
        full_history = _bars(timestamps)
        calls: list[dict[str, object]] = []

        class Adapter:
            def fetch_ohlcv(self, *_args, **kwargs):
                calls.append(kwargs)
                if kwargs["limit"] == 6:
                    return full_history.iloc[-3:].reset_index(drop=True)
                return full_history.copy()

        strategy = MagicMock()
        strategy.on_bar.return_value = []
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
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=5,
            ),
        )
        trader = LiveTrader(
            strategy,
            lambda frame: frame,
            config=config,
            adapter=Adapter(),
            clock=lambda: datetime(2025, 1, 4, tzinfo=UTC),
        )

        trader._poll_cycle()

        assert [call["limit"] for call in calls] == [6, 12]
        assert all(call["session_mode"] == "regular" for call in calls)
        assert trader.warmup_ready
        assert len(trader._ohlcv_cache["AAPL"]) == 5
        assert trader._last_bar_ts["AAPL"] == timestamps[-1]
        strategy.on_bar.assert_called_once()

    def test_bounded_shortfall_disables_all_symbols_without_advancing_watermarks(self):
        from librae.live.engine import LiveTrader

        full = _bars([datetime(2025, 1, day, tzinfo=UTC) for day in range(1, 6)])
        short = full.iloc[-2:].reset_index(drop=True)
        requests: list[tuple[str, int]] = []
        runtime_events = []
        feature = MagicMock(side_effect=lambda frame: frame)
        strategy = MagicMock()
        strategy.on_bar.return_value = []

        def fetcher(symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append((symbol, limit))
            return full.copy() if symbol == "BTCUSDT" else short.copy()

        trader = LiveTrader(
            strategy,
            feature,
            config=_test_cfg(
                symbols=["BTCUSDT", "ETHUSDT"],
                instrument_overrides={
                    "ETHUSDT": {
                        "instrument_type": "spot",
                        "currency": "USDT",
                    }
                },
                symbol_cost_overrides={"ETHUSDT": {"multiplier": 1.0}},
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                ),
            ),
            adapter=fetcher,
            on_runtime_event=runtime_events.append,
            clock=lambda: datetime(2025, 1, 7, tzinfo=UTC),
        )

        trader._poll_cycle()
        trader._poll_cycle()

        assert requests == [
            ("BTCUSDT", 6),
            ("ETHUSDT", 6),
            ("ETHUSDT", 12),
            ("BTCUSDT", 2),
            ("ETHUSDT", 12),
        ]
        assert not trader.warmup_ready
        feature.assert_not_called()
        strategy.on_bar.assert_not_called()
        assert trader._last_bar_ts == {}
        assert trader._last_cycle_ts is None
        assert len(runtime_events) == 1
        assert runtime_events[0].event_type == "decision_skipped"
        assert runtime_events[0].symbol == "ETHUSDT"
        assert runtime_events[0].detail == {
            "reason": "warmup_backfill_exhausted",
            "gap_reason": "warmup_incomplete",
            "usable_periods": 2,
            "required_periods": 5,
            "missing_periods": 3,
            "requested_periods": 12,
            "attempts": 2,
            "max_attempts": 3,
            "terminal": True,
        }

        # One symbol becoming ready must re-arm its own edge even while the
        # other symbol keeps the portfolio-level warmup gate closed.
        alerts: list[tuple[str, dict[str, object]]] = []
        trader._notify = lambda method, **kwargs: alerts.append((method, kwargs))
        trader._ohlcv_cache["BTCUSDT"] = short.copy()
        trader._report_incomplete_warmup()
        trader._ohlcv_cache["BTCUSDT"] = full.copy()
        trader._report_incomplete_warmup()
        trader._ohlcv_cache["BTCUSDT"] = short.copy()
        trader._report_incomplete_warmup()

        btc_events = [event for event in runtime_events if event.symbol == "BTCUSDT"]
        assert len(btc_events) == 2
        assert len(alerts) == 2

    def test_exhausted_backfill_rearms_only_after_new_history(self):
        from librae.live.engine import LiveTrader

        full = _bars([datetime(2025, 1, day, tzinfo=UTC) for day in range(1, 6)])
        usable_periods = [2]
        requests: list[int] = []

        def fetcher(_symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append(limit)
            return full.iloc[: usable_periods[0]].copy()

        trader = LiveTrader(
            MagicMock(),
            lambda history: history,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=fetcher,
            clock=lambda: datetime(2025, 1, 7, tzinfo=UTC),
        )

        trader._fetch_with_cache("BTCUSDT")
        trader._fetch_with_cache("BTCUSDT")
        usable_periods[0] = 3
        trader._fetch_with_cache("BTCUSDT")

        assert requests == [6, 12, 12, 12, 24]
        assert len(trader._ohlcv_cache["BTCUSDT"]) == 3
        assert "BTCUSDT" in trader._warmup_exhausted_fingerprints

    def test_replay_diagnostic_reports_history_missing_before_first_candidate(self):
        from librae.live.engine import LiveTrader

        history = _bars([datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(6)])
        runtime_events = []
        trader = LiveTrader(
            MagicMock(),
            lambda frame: frame,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=lambda *_args, **_kwargs: history.copy(),
            on_runtime_event=runtime_events.append,
            clock=lambda: datetime(2025, 1, 1, 7, tzinfo=UTC),
        )
        trader._ohlcv_cache["BTCUSDT"] = history.copy()
        trader._last_bar_ts["BTCUSDT"] = history.loc[1, "ts"].to_pydatetime()
        trader._last_cycle_ts = trader._last_bar_ts["BTCUSDT"]
        notifications: list[tuple[str, dict[str, object]]] = []
        trader._notify = lambda method, **kwargs: notifications.append((method, kwargs))

        trader._poll_cycle()

        assert len(runtime_events) == 1
        assert runtime_events[0].detail["gap_reason"] == "warmup_replay_history_incomplete"
        assert runtime_events[0].detail["usable_periods"] == 6
        assert runtime_events[0].detail["missing_periods"] == 2
        assert runtime_events[0].detail["terminal"] is True
        assert (
            "missing=2 periods before its next replay candidate" in notifications[0][1]["message"]
        )

    def test_exhausted_replay_detects_newly_available_older_history(self):
        from librae.live.engine import LiveTrader

        history = _bars([datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(7)])
        available_start = [2]
        requests: list[int] = []

        def fetcher(_symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append(limit)
            return history.iloc[available_start[0] :].copy()

        trader = LiveTrader(
            MagicMock(),
            lambda frame: frame,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=fetcher,
            clock=lambda: datetime(2025, 1, 1, 8, tzinfo=UTC),
        )
        trader._ohlcv_cache["BTCUSDT"] = history.iloc[2:].copy()
        trader._last_bar_ts["BTCUSDT"] = history.loc[4, "ts"].to_pydatetime()

        trader._fetch_with_cache("BTCUSDT")
        assert "BTCUSDT" in trader._warmup_exhausted_fingerprints

        available_start[0] = 1
        trader._fetch_with_cache("BTCUSDT")

        assert requests == [6, 6]
        assert trader._warmup_gap("BTCUSDT", trader._ohlcv_cache["BTCUSDT"]) is None
        assert "BTCUSDT" not in trader._warmup_exhausted_fingerprints

    def test_shadow_replay_backlog_is_bounded_until_operator_resynchronizes(self):
        from librae.live.engine import LiveTrader

        start = datetime(2025, 1, 1, tzinfo=UTC)
        history = _bars([start + pd.Timedelta(hours=offset) for offset in range(20)])
        latest_period = [2]
        requests: list[tuple[str, int]] = []
        runtime_events = []

        def fetcher(symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append((symbol, limit))
            if symbol == "BBB":
                return history.iloc[:1].copy()
            end = latest_period[0]
            return history.iloc[max(0, end - limit) : end].copy()

        trader = LiveTrader(
            MagicMock(),
            lambda frame: frame,
            config=_test_cfg(
                symbols=["AAA", "BBB"],
                instrument_overrides={
                    "AAA": {"instrument_type": "spot", "currency": "USDT"},
                    "BBB": {"instrument_type": "spot", "currency": "USDT"},
                },
                symbol_cost_overrides={
                    "AAA": {"multiplier": 1.0},
                    "BBB": {"multiplier": 1.0},
                },
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=2,
                ),
            ),
            adapter=fetcher,
            on_runtime_event=runtime_events.append,
            clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
        )
        trader._ohlcv_cache["AAA"] = history.iloc[:2].copy()
        trader._last_bar_ts["AAA"] = history.loc[1, "ts"].to_pydatetime()
        trader._last_cycle_ts = trader._last_bar_ts["AAA"]

        for end in range(3, 15):
            latest_period[0] = end
            trader._poll_cycle()

        max_rows = (trader._warmup_periods + 1) * trader.WARMUP_MAX_FETCH_MULTIPLIER
        aaa_requests_before_terminal_poll = sum(symbol == "AAA" for symbol, _ in requests)
        trader._poll_cycle()

        assert len(trader._ohlcv_cache["AAA"]) <= max_rows
        assert "AAA" in trader._replay_backlog_exhausted
        assert sum(symbol == "AAA" for symbol, _ in requests) == aaa_requests_before_terminal_poll
        backlog_events = [
            event
            for event in runtime_events
            if event.detail["reason"] == "warmup_replay_backlog_exhausted"
        ]
        assert len(backlog_events) == 1
        assert backlog_events[0].detail["terminal"] is True
        assert not trader.warmup_ready

    def test_partial_warmup_progress_does_not_repeat_outward_alerts(self):
        from librae.live.engine import LiveTrader

        full = _bars([datetime(2025, 1, day, tzinfo=UTC) for day in range(1, 6)])
        usable_periods = [2]
        runtime_events = []
        alerts: list[tuple[str, dict[str, object]]] = []
        strategy = MagicMock()
        strategy.on_bar.return_value = []

        def fetcher(*_args, **_kwargs):
            return full.iloc[-usable_periods[0] :].reset_index(drop=True)

        trader = LiveTrader(
            strategy,
            lambda history: history,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=fetcher,
            on_runtime_event=runtime_events.append,
            clock=lambda: datetime(2025, 1, 5, 1, tzinfo=UTC),
        )
        trader._notify = lambda method, **kwargs: alerts.append((method, kwargs))

        for count in (2, 3, 4):
            usable_periods[0] = count
            trader._poll_cycle()

        assert [event.detail["usable_periods"] for event in runtime_events] == [2]
        assert len(alerts) == 1
        strategy.on_bar.assert_not_called()

        usable_periods[0] = 5
        trader._poll_cycle()
        assert trader.warmup_ready
        strategy.on_bar.assert_called_once()

        trader._ohlcv_cache["BTCUSDT"] = full.iloc[-2:].reset_index(drop=True)
        usable_periods[0] = 2
        trader._poll_cycle()

        assert [event.detail["usable_periods"] for event in runtime_events] == [2, 2]
        assert len(alerts) == 2

    def test_source_unavailable_rows_do_not_satisfy_warmup(self):
        from librae.live.engine import LiveTrader

        now = datetime(2025, 1, 1, 5, tzinfo=UTC)
        frame = _bars([datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(5)])
        frame["available_at"] = pd.DatetimeIndex(
            [datetime(2025, 1, 1, hour + 1, tzinfo=UTC) for hour in range(4)]
            + [datetime(2025, 1, 1, 6, tzinfo=UTC)]
        )
        runtime_events = []
        strategy = MagicMock()
        strategy.on_bar.return_value = []
        trader = LiveTrader(
            strategy,
            lambda history: history,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=lambda *_args, **_kwargs: frame,
            on_runtime_event=runtime_events.append,
            clock=lambda: now,
        )

        trader._poll_cycle()

        assert not trader.warmup_ready
        assert len(trader._ohlcv_cache["BTCUSDT"]) == 4
        strategy.on_bar.assert_not_called()
        assert runtime_events[0].detail["usable_periods"] == 4

    def test_recursive_feature_uses_fixed_window_after_recovery_and_increment(self):
        from librae.live.engine import LiveTrader

        timestamps = [datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(6)]
        full = _bars(timestamps, [100.0, 102.0, 101.0, 105.0, 104.0, 108.0])
        requests: list[int] = []
        feature_inputs: list[list[float]] = []
        observed_recursive: list[float] = []

        def fetcher(_symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append(limit)
            if requests == [6]:
                return full.iloc[2:5].reset_index(drop=True)
            if limit == 12:
                return full.iloc[:5].copy()
            return full.iloc[-2:].copy()

        def recursive_feature(frame: pd.DataFrame) -> pd.DataFrame:
            feature_inputs.append(frame["close"].tolist())
            result = frame.copy()
            result["recursive"] = result["close"].ewm(alpha=0.5, adjust=False).mean()
            return result

        class CaptureStrategy:
            def on_bar(self, ctx):
                observed_recursive.append(float(ctx.bar["recursive"]))
                return []

        trader = LiveTrader(
            CaptureStrategy(),
            recursive_feature,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=5,
                )
            ),
            adapter=fetcher,
            clock=lambda: datetime(2025, 1, 1, 7, tzinfo=UTC),
        )

        trader._poll_cycle()
        trader._poll_cycle()

        assert requests == [6, 12, 2]
        assert feature_inputs == [
            [100.0, 102.0, 101.0, 105.0, 104.0],
            [102.0, 101.0, 105.0, 104.0, 108.0],
        ]
        assert len(observed_recursive) == 2
        assert len(trader._ohlcv_cache["BTCUSDT"]) == 5

    def test_restored_simulation_backfills_pre_warmup_history_before_replay(self):
        from librae.live.engine import LiveTrader

        timestamps = [datetime(2025, 1, 1, hour, tzinfo=UTC) for hour in range(7)]
        full = _bars(timestamps)
        config = _test_cfg(
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=5,
            )
        )
        store = MemoryLiveStateStore()
        first_strategy = MagicMock()
        first_strategy.on_bar.return_value = []
        first = LiveTrader(
            first_strategy,
            lambda frame: frame,
            config=config,
            adapter=lambda *_args, **_kwargs: full.iloc[:5].copy(),
            state_store=store,
            clock=lambda: datetime(2025, 1, 1, 8, tzinfo=UTC),
        )
        first._poll_cycle()
        assert first._last_bar_ts["BTCUSDT"] == timestamps[4]

        requests: list[int] = []
        replay_windows: list[list[datetime]] = []
        second_strategy = MagicMock()
        second_strategy.on_bar.return_value = []

        def restored_fetcher(_symbol: str, _timeframe: str, limit: int, **_kwargs):
            requests.append(limit)
            return full.iloc[-limit:].copy()

        def capture_window(frame: pd.DataFrame) -> pd.DataFrame:
            replay_windows.append([value.to_pydatetime() for value in frame.index])
            return frame

        restored = LiveTrader(
            second_strategy,
            capture_window,
            config=config,
            adapter=restored_fetcher,
            state_store=store,
            clock=lambda: datetime(2025, 1, 1, 8, tzinfo=UTC),
        )

        restored._poll_cycle()

        assert requests == [6]
        assert replay_windows == [timestamps[1:6], timestamps[2:7]]
        assert second_strategy.on_bar.call_count == 2
        assert restored._last_bar_ts["BTCUSDT"] == timestamps[6]
        assert len(restored._ohlcv_cache["BTCUSDT"]) == 5
