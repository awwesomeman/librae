"""Missing (NaN) volume means the source reports none, as for an index.

Such bars must load and drive prices and signals, while nothing that needs
volume reads the gap as zero: a volume cap, impact, or ADV rejects the fill
instead, and persisted state keeps the gap as a gap.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.liquidity import calculate_lagged_adv
from librae.core.market_data import MarketDataSubscription
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import Context, OrderIntent, PortfolioWeights, Strategy
from librae.core.trading_calendar import resample_session_ohlcv
from librae.db.timescale_writer import write_run_bars
from librae.live.state import LiveRebalance, PendingOhlcvDelivery

from tests.conftest import declare_instruments, primary_subscriptions_for

NAN = float("nan")


def _bars(volume: list[float], *, symbol: str = "BTCUSDT", freq: str = "D") -> pd.DataFrame:
    n = len(volume)
    index = pd.MultiIndex.from_arrays(
        [[symbol] * n, pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")],
        names=["symbol", "datetime"],
    )
    return pd.DataFrame(
        {
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.0] * n,
            "volume": volume,
        },
        index=index,
    )


class _BuyAt(Strategy):
    """Buys ``quantity`` of ``symbol`` when deciding on each listed bar."""

    def __init__(self, periods: tuple[int, ...], quantity: float, symbol: str = "BTCUSDT"):
        self._periods = periods
        self._quantity = quantity
        self._symbol = symbol

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index in self._periods:
            return [OrderIntent(action="long", symbol=self._symbol, quantity=self._quantity)]
        return []


def _entry_fills(data: pd.DataFrame, strategy: Strategy, policy: ExecutionPolicy) -> list[float]:
    result = Backtest(
        data,
        strategy,
        initial_balance=1_000_000.0,
        cost_model=CostModel.zero(),
        execution=policy,
        primary_subscriptions=primary_subscriptions_for(data),
        instrument_overrides=declare_instruments(data, currency="USDT"),
        currency="USDT",
    ).run()
    return [
        event.fill_quantity
        for event in result.position_events
        if event.event_type in ("open", "add")
    ]


class TestTheBacktestBoundary:
    def test_missing_volume_loads_and_fills_without_a_volume_cap(self) -> None:
        data = _bars([NAN] * 6)

        fills = _entry_fills(
            data, _BuyAt((1,), 10.0), ExecutionPolicy(max_bar_volume_participation_rate=None)
        )

        assert fills == pytest.approx([10.0])

    def test_a_volume_cap_rejects_a_fill_whose_reference_volume_is_missing(self) -> None:
        # The fill after bar 1 is capped by bar 1's volume, which is missing;
        # the fill after bar 2 is capped by 50% of bar 2's 100.
        data = _bars([100.0, NAN, 100.0, 100.0, 100.0])

        fills = _entry_fills(
            data, _BuyAt((1, 2), 80.0), ExecutionPolicy(max_bar_volume_participation_rate=0.5)
        )

        assert fills == pytest.approx([50.0])

    def test_an_index_without_volume_can_drive_a_traded_symbol(self) -> None:
        data = pd.concat([_bars([NAN] * 6, symbol="^GSPC"), _bars([1_000.0] * 6, symbol="BTCUSDT")])

        fills = _entry_fills(data, _BuyAt((1,), 10.0), ExecutionPolicy())

        assert fills == pytest.approx([10.0])

    def test_adv_rejects_a_window_containing_a_session_without_volume(self) -> None:
        # ADV for the fill on bar 3 averages bars 1-2 (bar 1 missing): none.
        # ADV for the fill on bar 4 averages bars 2-3: 300, so 10% is 30.
        data = _bars([100.0, NAN, 300.0, 300.0, 300.0, 300.0])
        policy = ExecutionPolicy(
            max_bar_volume_participation_rate=1.0,
            adv_lookback_sessions=2,
            max_adv_participation_rate=0.1,
        )

        assert _entry_fills(data, _BuyAt((2, 3), 1_000.0), policy) == pytest.approx([30.0])

    @pytest.mark.parametrize(
        ("column", "value", "message"),
        [
            ("volume", math.inf, "volume must be finite or missing"),
            ("volume", -1.0, "volume must be non-negative"),
            ("close", NAN, "OHLC prices must be finite"),
            ("open", math.inf, "OHLC prices must be finite"),
        ],
    )
    def test_other_invalid_values_are_still_rejected(
        self, column: str, value: float, message: str
    ) -> None:
        data = _bars([100.0] * 6)
        data.iloc[1, data.columns.get_loc(column)] = value

        with pytest.raises(ValueError, match=message):
            Backtest(
                data,
                _BuyAt((), 0.0),
                cost_model=CostModel.zero(),
            )


class TestLaggedAdv:
    def test_a_daily_window_with_missing_volume_has_no_adv(self) -> None:
        result = calculate_lagged_adv(pd.Series([100.0, NAN, 300.0, 400.0, 500.0]), 2)

        assert result.iloc[:4].isna().all()
        assert result.iloc[4] == pytest.approx(350.0)

    def test_one_missing_bar_leaves_its_session_without_volume(self) -> None:
        volume = pd.Series([40.0, NAN, 100.0, 200.0, 50.0, 50.0, 1.0])
        labels = pd.Index(["d1", "d1", "d2", "d2", "d3", "d3", "d4"])

        result = calculate_lagged_adv(volume, 2, session_labels=labels)

        # d3 averages d1 (unknown) and d2; d4 averages d2 (300) and d3 (100).
        assert result.iloc[:6].isna().all()
        assert result.iloc[6] == pytest.approx(200.0)


def test_a_resampled_bucket_with_any_missing_minute_has_missing_volume() -> None:
    minutes = pd.date_range("2025-01-01", periods=120, freq="min", tz="UTC")
    volume = np.full(120, 2.0)
    volume[30] = NAN
    frame = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": volume}, index=minutes
    )

    result = resample_session_ohlcv(frame, 60 * 60, "24/7")

    assert math.isnan(result["volume"].iloc[0])
    assert result["volume"].iloc[1] == pytest.approx(120.0)


class TestPersistedState:
    def test_a_pending_bar_without_volume_round_trips_as_strict_json(self) -> None:
        at = datetime(2025, 1, 1, 1, tzinfo=UTC)
        pending = PendingOhlcvDelivery(
            subscription=MarketDataSubscription(
                symbol="^GSPC",
                timeframe="H1",
                calendar_id="24/7",
                session_mode="extended",
                data_source="test",
                instrument_type="spot",
            ),
            ts=at - timedelta(hours=1),
            available_at=at,
            observed_at=at,
            bar={"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": NAN},
        )

        # Postgres jsonb refuses a NaN token, so the checkpoint must not carry one.
        document = json.loads(json.dumps(pending.to_dict(), allow_nan=False))
        restored = PendingOhlcvDelivery.from_dict(document)

        assert math.isnan(restored.bar["volume"])
        assert restored.bar["close"] == 1.0

    def test_a_missing_reference_volume_is_stored_as_null(self) -> None:
        rebalance = LiveRebalance(
            targets=PortfolioWeights(weights={"AAA": 0.5, "BBB": 0.5}),
            reference_prices={"AAA": 100.0, "BBB": 100.0},
            reference_volumes={"AAA": NAN, "BBB": 1_000.0},
            lagged_adv_by_symbol={},
            decided_at=datetime(2025, 1, 1, tzinfo=UTC),
        )

        document = json.loads(json.dumps(rebalance.to_dict(), allow_nan=False))

        assert LiveRebalance.from_dict(document).reference_volumes == {
            "AAA": None,
            "BBB": 1_000.0,
        }


class TestSimPolling:
    RECEIVED = datetime(2025, 1, 1, 10, tzinfo=UTC)

    @staticmethod
    def _frame() -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ts": pd.date_range("2025-01-01T00:00Z", periods=6, freq="h", tz="UTC"),
                "open": [100.0] * 6,
                "high": [101.0] * 6,
                "low": [99.0] * 6,
                "close": [100.0] * 6,
                "volume": [NAN] * 6,
            }
        )

    def test_a_bar_without_volume_fetched_again_is_not_a_revision(self, caplog) -> None:
        from tests.engine.live_factories import _test_cfg, make_live_runner

        delivered: list[tuple[datetime, dict]] = []

        def sink(_symbol, _timeframe, bar, ts) -> None:
            delivered.append((ts, dict(bar)))

        sink.durable_ohlcv_delivery = True
        clock = {"now": self.RECEIVED}
        runner = make_live_runner(
            config=_test_cfg(mode="sim", warmup_periods=1),
            fetcher=lambda *a, **k: self._frame(),
            on_ohlcv=sink,
            clock=lambda: clock["now"],
        )

        with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
            runner._poll_cycle()
            clock["now"] = self.RECEIVED + timedelta(minutes=30)
            runner._poll_cycle()

        last_bar = datetime(2025, 1, 1, 5, tzinfo=UTC)
        versions = [bar for ts, bar in delivered if ts == last_bar]
        assert len(versions) == 1
        assert math.isnan(versions[0]["volume"])
        assert "changed under the same available_at" not in caplog.text


def test_run_bars_store_missing_volume_as_null() -> None:
    """NULL, unlike NaN, is skipped by SQL aggregates over the stored bars."""
    frame = _bars([NAN, 5.0], freq="h").droplevel("symbol").rename_axis("ts")
    subscription = MarketDataSubscription(
        symbol="BTCUSDT",
        timeframe="H1",
        calendar_id="24/7",
        session_mode="extended",
        data_source="test",
        instrument_type="spot",
    )

    with (
        patch("librae.db.timescale_writer.get_conn"),
        patch(
            "librae.db.timescale_writer.psycopg2.extras.execute_values", return_value=[(1,), (1,)]
        ) as execute_values,
    ):
        write_run_bars("run-1", subscription, frame)

    rows = execute_values.call_args.args[2]
    assert rows[0][-1] is None
    assert rows[1][-1] == 5.0
