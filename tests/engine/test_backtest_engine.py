"""Tests for the Backtest engine: Strategy protocol + Executor pattern."""

from __future__ import annotations

import exchange_calendars as xcals
import librae
import numpy as np
import pandas as pd
import pytest
from librae import normalize_bars
from librae.backtest.engine import Backtest
from librae.backtest.result import BacktestResult
from librae.core.cost_model import CostModel
from librae.core.run_config import AccountConfig, ExecutionPolicy, RiskPolicy
from librae.core.strategy import Context, OrderIntent, Strategy

from tests.conftest import make_test_cfg

# ── Helpers ───────────────────────────────────────────────────────────────


def test_result_model_is_reexported_from_public_api() -> None:
    assert librae.BacktestResult is BacktestResult


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


def _xnys_session_opens(start: str, end: str) -> pd.DatetimeIndex:
    schedule = xcals.get_calendar("XNYS").schedule.loc[start:end]
    return pd.DatetimeIndex(schedule["open"])


def _first_observation_per_period(
    index: pd.DatetimeIndex,
    frequency: str,
) -> pd.DatetimeIndex:
    periods = pd.PeriodIndex(index.tz_convert(None), freq=frequency)
    return index[~periods.duplicated()]


def _frame_at_timestamps(
    symbol: str,
    timestamps: pd.DatetimeIndex,
    *,
    price: float = 100.0,
) -> pd.DataFrame:
    frame = _make_multiindex_df([price] * len(timestamps), symbol=symbol)
    frame.index = pd.MultiIndex.from_arrays(
        [[symbol] * len(timestamps), timestamps],
        names=["symbol", "datetime"],
    )
    return frame


_XNYS_SESSION_CADENCE_TIMESTAMPS = {
    "D1": (
        "2026-03-05 14:30Z",
        "2026-03-06 14:30Z",
        "2026-03-09 13:30Z",
        "2026-03-10 13:30Z",
        "2026-03-11 13:30Z",
    ),
    "W1": (
        "2026-02-23 14:30Z",
        "2026-03-02 14:30Z",
        "2026-03-09 13:30Z",
        "2026-03-16 13:30Z",
        "2026-03-23 13:30Z",
    ),
    "MN1": (
        "2026-01-02 14:30Z",
        "2026-02-02 14:30Z",
        "2026-03-02 14:30Z",
        "2026-04-01 13:30Z",
        "2026-05-01 13:30Z",
    ),
    "D5": (
        "2026-02-17 14:30Z",
        "2026-02-24 14:30Z",
        "2026-03-03 14:30Z",
        "2026-03-10 13:30Z",
        "2026-03-17 13:30Z",
    ),
    "D10": (
        "2026-02-17 14:30Z",
        "2026-03-03 14:30Z",
        "2026-03-17 13:30Z",
        "2026-03-31 13:30Z",
        "2026-04-15 13:30Z",
    ),
    "D21": (
        "2026-01-06 14:30Z",
        "2026-02-05 14:30Z",
        "2026-03-09 13:30Z",
        "2026-04-08 13:30Z",
        "2026-05-07 13:30Z",
    ),
    "W5": (
        "2026-01-05 14:30Z",
        "2026-02-09 14:30Z",
        "2026-03-16 13:30Z",
        "2026-04-20 13:30Z",
        "2026-05-26 13:30Z",
    ),
}


def _assert_terminal_equity_reconciles(
    backtest: Backtest,
    result: BacktestResult,
    expected_equity: float,
) -> None:
    output = backtest.build_output()
    assert result.final_equity == pytest.approx(expected_equity)
    assert result.equity_curve[-1].equity == pytest.approx(expected_equity)
    assert output.account.final_equity == pytest.approx(expected_equity)
    assert output.account.net_pnl == pytest.approx(expected_equity - result.initial_cash)
    assert output.metrics.total_return == pytest.approx(expected_equity / result.initial_cash - 1.0)


# ── Strategies for testing ───────────────────────────────────────────────


class HoldStrategy(Strategy):
    """Never trades."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


class BuyBar2CloseBar4(Strategy):
    """Buy at bar 2, close at bar 4."""

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        if ctx.period_index == 2 and ctx.symbol not in ctx.positions:
            return [OrderIntent(action="long", symbol=ctx.symbol)]
        if ctx.period_index == 4 and ctx.symbol in ctx.positions:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return []


class SignalDrivenStrategy(Strategy):
    """Trades based on entry_signal / exit_signal columns in df."""

    def __init__(self, max_hold_periods: int = 24):
        self.max_hold_periods = max_hold_periods

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        pos = ctx.positions.get(ctx.symbol)
        if pos:
            if ctx.bar["exit_signal"] or pos.periods_held >= self.max_hold_periods:
                return [OrderIntent(action="close", symbol=ctx.symbol)]
        elif ctx.bar["entry_signal"]:
            return [OrderIntent(action="long", symbol=ctx.symbol)]
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

    def test_direct_constructor_uses_execution_defaults(self) -> None:
        class BuyOnce(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=100.0)]
                return []

        result = Backtest(
            _make_multiindex_df([100.0] * 5),
            BuyOnce(),
            initial_balance=100_000.0,
            cost_model=_zero_cost(),
        ).run()

        open_event = next(event for event in result.position_events if event.event_type == "open")
        assert open_event.fill_quantity == pytest.approx(10.0)

    def test_configured_price_grid_rejects_intent_before_backtest_fill(self) -> None:
        class HalfTickLimitBuy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 0:
                    return [
                        OrderIntent(
                            action="long",
                            symbol=ctx.symbol,
                            quantity=1.0,
                            limit_price=100.125,
                        )
                    ]
                return []

        backtest = Backtest(
            _make_multiindex_df([100.0] * 5),
            HalfTickLimitBuy(),
            config=make_test_cfg(instrument_overrides={"BTCUSDT": {"price_increment": 0.25}}),
        )

        with pytest.raises(ValueError, match=r"limit_price.*price_increment"):
            backtest.run()

    def test_configured_minimum_notional_skips_backtest_entry(self) -> None:
        class SmallBuy(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        result = Backtest(
            _make_multiindex_df([100.0] * 5),
            SmallBuy(),
            config=make_test_cfg(instrument_overrides={"BTCUSDT": {"min_notional": 150.0}}),
        ).run()

        assert result.position_events == []
        assert any(
            event.detail.get("reason") == "notional_below_minimum"
            for event in result.runtime_events
        )

    @pytest.mark.parametrize(
        "initial_balance",
        [0.0, -1.0, float("nan"), float("inf"), True, "100000"],
    )
    def test_direct_constructor_rejects_invalid_initial_balance(
        self,
        initial_balance: float,
    ) -> None:
        with pytest.raises(ValueError, match="initial_balance"):
            Backtest(
                _make_multiindex_df([100.0, 101.0]),
                HoldStrategy(),
                initial_balance=initial_balance,
            )

    @pytest.mark.parametrize(
        ("override_name", "override"),
        [
            ("execution", ExecutionPolicy()),
            ("risk", RiskPolicy(max_position_weight=0.5)),
        ],
    )
    def test_config_rejects_second_policy_source(self, override_name, override) -> None:
        with pytest.raises(ValueError, match=override_name):
            Backtest(
                _make_multiindex_df([100.0, 101.0]),
                HoldStrategy(),
                config=make_test_cfg(),
                **{override_name: override},
            )

    def test_d1_adv_limit_uses_only_completed_sessions(self) -> None:
        class BuyAfterAdvWarmup(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 2:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=100.0)]
                return []

        timestamps = pd.date_range("2025-01-01", periods=5, freq="D", tz="UTC")
        index = pd.MultiIndex.from_arrays(
            [["BTCUSDT"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        data = pd.DataFrame(
            {
                "open": [100.0] * 5,
                "high": [101.0] * 5,
                "low": [99.0] * 5,
                "close": [100.0] * 5,
                "volume": [100.0, 200.0, 300.0, 1_000.0, 1_000.0],
            },
            index=index,
        )
        policy = ExecutionPolicy(
            max_bar_volume_participation_rate=0.5,
            adv_lookback_sessions=3,
            max_adv_participation_rate=0.1,
        )

        result = Backtest(
            data,
            BuyAfterAdvWarmup(),
            initial_balance=100_000.0,
            cost_model=_zero_cost(),
            execution=policy,
        ).run()

        open_event = next(event for event in result.position_events if event.event_type == "open")
        # Execution bar volume is 1,000, but lagged ADV is (100+200+300)/3=200.
        assert open_event.fill_quantity == pytest.approx(20.0)

    def test_intraday_adv_budget_is_cumulative_across_session_bars(self) -> None:
        class AddTwiceAfterWarmup(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index in (3, 4):
                    return [
                        OrderIntent(
                            action="long",
                            symbol=ctx.symbol,
                            quantity=15.0,
                        )
                    ]
                return []

        timestamps = pd.DatetimeIndex(
            [
                "2025-01-01 00:00Z",
                "2025-01-01 01:00Z",
                "2025-01-02 00:00Z",
                "2025-01-02 01:00Z",
                "2025-01-03 00:00Z",
                "2025-01-03 01:00Z",
                "2025-01-04 00:00Z",
                "2025-01-04 01:00Z",
            ]
        )
        index = pd.MultiIndex.from_arrays(
            [["BTCUSDT"] * len(timestamps), timestamps],
            names=["symbol", "datetime"],
        )
        data = pd.DataFrame(
            {
                "open": [100.0] * len(timestamps),
                "high": [101.0] * len(timestamps),
                "low": [99.0] * len(timestamps),
                "close": [100.0] * len(timestamps),
                "volume": [100.0] * 4 + [1_000.0] * 4,
            },
            index=index,
        )
        policy = ExecutionPolicy(
            max_bar_volume_participation_rate=1.0,
            adv_lookback_sessions=2,
            max_adv_participation_rate=0.1,
        )
        result = Backtest(
            data,
            AddTwiceAfterWarmup(),
            initial_balance=100_000.0,
            cost_model=_zero_cost(),
            execution=policy,
        ).run()

        entry_events = [
            event for event in result.position_events if event.event_type in ("open", "add")
        ]
        assert [event.fill_quantity for event in entry_events] == pytest.approx([15.0, 5.0])

    def test_intraday_adv_requires_symbol_calendar(self) -> None:
        policy = ExecutionPolicy(
            adv_lookback_sessions=2,
            max_adv_participation_rate=0.1,
        )
        backtest = Backtest(
            _make_multiindex_df([100.0] * 5, symbol="UNREGISTERED"),
            HoldStrategy(),
            execution=policy,
        )

        with pytest.raises(ValueError, match=r"calendar_id.*UNREGISTERED"):
            backtest.run()

    def test_intraday_adv_uses_run_calendar_for_unregistered_symbol(self) -> None:
        policy = ExecutionPolicy(
            adv_lookback_sessions=2,
            max_adv_participation_rate=0.1,
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["UNREGISTERED"],
            calendar_id="24/7",
            instrument_overrides={
                "UNREGISTERED": {
                    "data_adapter": "crypto",
                    "instrument_type": "spot",
                    "currency": "USDT",
                }
            },
            symbol_cost_overrides={"UNREGISTERED": {"multiplier": 1.0}},
            execution=policy,
        )
        backtest = Backtest(
            _make_multiindex_df([100.0] * 5, symbol="UNREGISTERED"),
            HoldStrategy(),
            config=config,
        )

        backtest.run()

        assert backtest._calendar_ids == {"UNREGISTERED": "24/7"}

    def test_force_close_at_end(self) -> None:
        prices = [100.0, 100.0, 100.0, 110.0, 120.0]
        df = _make_multiindex_df(prices)

        class BuyBar2(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 2 and ctx.symbol not in ctx.positions:
                    return [OrderIntent(action="long", symbol=ctx.symbol)]
                return []

        bt = Backtest(
            df, BuyBar2(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        result = bt.run()

        assert len(result.trades) == 1
        assert np.isclose(result.trades[0].exit_price, 120.0)

    def test_final_turnover_includes_same_bar_fill_and_forced_close(self) -> None:
        prices = [100.0] * 5
        df = _make_multiindex_df(prices)

        class OpenThenAdd(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                if ctx.period_index == 3:
                    return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
                return []

        result = Backtest(
            df,
            OpenThenAdd(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        assert result.portfolio_snapshots[-1].turnover == pytest.approx(0.3)
        assert result.portfolio_snapshots[-1].gross_exposure == 0.0
        assert result.portfolio_snapshots[-1].exposed is True

    def test_requires_multiindex(self) -> None:
        df = pd.DataFrame({"close": [100.0]}, index=pd.date_range("2025-01-01", periods=1))
        with pytest.raises(ValueError, match="MultiIndex"):
            Backtest(df, HoldStrategy(), data_source="test")


class TestBacktestDataContract:
    def test_requires_exact_index_names(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df.index = df.index.set_names(["asset", "ts"])

        with pytest.raises(ValueError, match=r"exactly \('symbol', 'datetime'\)"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_requires_all_ohlcv_columns(self) -> None:
        df = _make_multiindex_df([100.0] * 5).drop(columns="volume")

        with pytest.raises(ValueError, match="missing required OHLCV columns: volume"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_duplicate_symbol_timestamp(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df = pd.concat([df, df.iloc[[0]]])

        with pytest.raises(ValueError, match=r"unique \(symbol, datetime\)"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_timezone_naive_timestamps(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        naive = df.index.get_level_values("datetime").tz_localize(None)
        df.index = pd.MultiIndex.from_arrays(
            [df.index.get_level_values("symbol"), naive],
            names=["symbol", "datetime"],
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_non_monotonic_timestamps_within_symbol(self) -> None:
        df = _make_multiindex_df([100.0] * 5).iloc[[1, 0, 2, 3, 4]]

        with pytest.raises(ValueError, match="increasing within symbol"):
            Backtest(df, HoldStrategy(), data_source="test")

    @pytest.mark.parametrize("value", [np.nan, np.inf, -1.0, 0.0])
    def test_rejects_invalid_prices(self, value: float) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df.iloc[0, df.columns.get_loc("open")] = value

        with pytest.raises(ValueError, match=r"finite|positive"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_inconsistent_ohlc(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df.iloc[0, df.columns.get_loc("high")] = 99.0

        with pytest.raises(ValueError, match="OHLC values are inconsistent"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_negative_volume(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df.iloc[0, df.columns.get_loc("volume")] = -1.0

        with pytest.raises(ValueError, match="volume must be non-negative"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_non_boolean_side_tradability(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df["can_buy"] = "false"
        df["can_sell"] = True

        with pytest.raises(ValueError, match="must contain non-null booleans"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_rejects_incomplete_side_tradability(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        df["can_buy"] = True

        with pytest.raises(ValueError, match="must provide can_buy and can_sell together"):
            Backtest(df, HoldStrategy(), data_source="test")

    def test_cfg_symbols_must_match_data(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        cfg = make_test_cfg(mode="backtest", symbols=["ETHUSDT"])

        with pytest.raises(ValueError, match="must exactly match data symbols"):
            Backtest(df, HoldStrategy(), config=cfg, cost_model=_zero_cost())

    def test_timeframe_is_inferred_per_symbol_not_from_staggered_event_union(self) -> None:
        aaa = _make_multiindex_df([100.0] * 5, symbol="AAA")
        bbb = _make_multiindex_df([200.0] * 5, symbol="BBB")
        shifted = bbb.index.get_level_values("datetime") + pd.Timedelta(minutes=30)
        bbb.index = pd.MultiIndex.from_arrays(
            [bbb.index.get_level_values("symbol"), shifted],
            names=["symbol", "datetime"],
        )
        backtest = Backtest(
            pd.concat([aaa, bbb]),
            HoldStrategy(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
        )

        backtest.run()

        assert backtest._timeframe == "H1"

    def test_rejects_inconsistent_per_symbol_timeframes(self) -> None:
        hourly = _make_multiindex_df([100.0] * 5, symbol="AAA")
        two_hour = _make_multiindex_df([200.0] * 5, symbol="BBB")
        timestamps = pd.date_range("2025-01-01", periods=5, freq="2h", tz="UTC")
        two_hour.index = pd.MultiIndex.from_arrays(
            [two_hour.index.get_level_values("symbol"), timestamps],
            names=["symbol", "datetime"],
        )

        with pytest.raises(ValueError, match="inconsistent timeframes"):
            Backtest(
                pd.concat([hourly, two_hour]),
                HoldStrategy(),
                initial_balance=1_000.0,
                cost_model=_zero_cost(),
            ).run()

    def test_rejects_configured_timeframe_that_disagrees_with_data(self) -> None:
        config = make_test_cfg(mode="backtest", timeframe="D1")

        with pytest.raises(ValueError, match=r"config\.timeframe=D1"):
            Backtest(
                _make_multiindex_df([100.0] * 5),
                HoldStrategy(),
                config=config,
                cost_model=_zero_cost(),
            ).run()

    def test_monthly_timeframe_accepts_variable_calendar_month_lengths(self) -> None:
        frame = _make_multiindex_df([100.0] * 5)
        monthly = pd.date_range("2025-01-01", periods=5, freq="MS", tz="UTC")
        frame.index = pd.MultiIndex.from_arrays(
            [frame.index.get_level_values("symbol"), monthly],
            names=["symbol", "datetime"],
        )
        backtest = Backtest(
            frame,
            HoldStrategy(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
        )

        backtest.run()

        assert backtest._timeframe == "MN1"

    def test_direct_input_is_canonicalized_to_utc_without_mutating_caller(self) -> None:
        frame = _make_multiindex_df([100.0] * 5)
        local_timestamps = frame.index.get_level_values("datetime").tz_convert("Asia/Taipei")
        frame.index = pd.MultiIndex.from_arrays(
            [frame.index.get_level_values("symbol"), local_timestamps],
            names=["symbol", "datetime"],
        )

        backtest = Backtest(frame, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert str(backtest._data.index.get_level_values("datetime").tz) == "UTC"
        assert str(frame.index.get_level_values("datetime").tz) == "Asia/Taipei"

    def test_normalized_input_keeps_the_same_canonical_utc_boundary(self) -> None:
        timestamps = pd.date_range(
            "2026-01-01",
            periods=5,
            freq="h",
            tz="Asia/Taipei",
        )
        frame = pd.DataFrame(
            {
                "open": [100.0] * 5,
                "high": [101.0] * 5,
                "low": [99.0] * 5,
                "close": [100.0] * 5,
                "volume": [100.0] * 5,
            },
            index=timestamps,
        )

        normalized = normalize_bars(frame, symbol="BTCUSDT")
        backtest = Backtest(normalized, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert backtest._data is normalized
        assert str(backtest._timeline[0].tz) == "UTC"

    @pytest.mark.parametrize(
        "timestamps",
        [
            [
                "2026-03-05 14:30Z",
                "2026-03-06 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-10 13:30Z",
                "2026-03-11 13:30Z",
            ],
            [
                "2026-10-29 13:30Z",
                "2026-10-30 13:30Z",
                "2026-11-02 14:30Z",
                "2026-11-03 14:30Z",
                "2026-11-04 14:30Z",
            ],
        ],
    )
    def test_xnys_daily_timeframe_accepts_dst_transitions(
        self,
        timestamps: list[str],
    ) -> None:
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, pd.to_datetime(timestamps, utc=True)],
            names=["symbol", "datetime"],
        )

        backtest = Backtest(frame, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "D1"

    def test_configured_xnys_daily_timeframe_accepts_sparse_sessions_across_dst(
        self,
    ) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-03-06 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-12 13:30Z",
                "2026-03-17 13:30Z",
                "2026-03-23 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "D1"

    def test_xnys_daily_timeframe_rejects_non_open_anchor(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-03-09 15:30Z",
                "2026-03-10 15:30Z",
                "2026-03-11 15:30Z",
                "2026-03-12 15:30Z",
                "2026-03-13 15:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )

        with pytest.raises(ValueError, match=r"canonical timeframe=D1 period starts"):
            Backtest(frame, HoldStrategy(), cost_model=_zero_cost()).run()

    def test_xnys_daily_timeframe_rejects_off_session_timestamp(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-03-05 14:30Z",
                "2026-03-06 14:30Z",
                "2026-03-07 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-10 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match="outside the XNYS trading session"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    @pytest.mark.parametrize(
        "timestamps",
        [
            [
                "2026-02-23 14:30Z",
                "2026-03-02 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-16 13:30Z",
                "2026-03-23 13:30Z",
            ],
            [
                "2026-10-19 13:30Z",
                "2026-10-26 13:30Z",
                "2026-11-02 14:30Z",
                "2026-11-09 14:30Z",
                "2026-11-16 14:30Z",
            ],
        ],
    )
    def test_xnys_weekly_timeframe_accepts_dst_transitions(
        self,
        timestamps: list[str],
    ) -> None:
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, pd.to_datetime(timestamps, utc=True)],
            names=["symbol", "datetime"],
        )

        backtest = Backtest(frame, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "W1"

    def test_xnys_weekly_timeframe_accepts_sparse_calendar_weeks(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-02-23 14:30Z",
                "2026-03-09 13:30Z",
                "2026-03-16 13:30Z",
                "2026-03-30 13:30Z",
                "2026-04-06 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )

        backtest = Backtest(frame, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "W1"

    def test_xnys_weekly_timeframe_accepts_holiday_week_first_session(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-05-11 13:30Z",
                "2026-05-18 13:30Z",
                "2026-05-26 13:30Z",
                "2026-06-01 13:30Z",
                "2026-06-08 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )

        backtest = Backtest(frame, HoldStrategy(), cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "W1"

    def test_configured_xnys_weekly_timeframe_rejects_shifted_weekday_anchor(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-03-03 14:30Z",
                "2026-03-10 13:30Z",
                "2026-03-17 13:30Z",
                "2026-03-24 13:30Z",
                "2026-03-31 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="W1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"config\.timeframe=W1"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    def test_configured_xnys_monthly_timeframe_uses_first_session(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-01-02 14:30Z",
                "2026-02-02 14:30Z",
                "2026-03-02 14:30Z",
                "2026-04-01 13:30Z",
                "2026-05-01 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="MN1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "MN1"

    @pytest.mark.parametrize(
        ("configured_timeframe", "actual_timeframe"),
        [
            (configured, actual)
            for configured in ("D1", "W1", "MN1")
            for actual in ("D1", "W1", "MN1")
        ],
    )
    def test_configured_session_timeframe_discriminates_calendar_cadence(
        self,
        configured_timeframe: str,
        actual_timeframe: str,
    ) -> None:
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [
                ["MU"] * 5,
                pd.to_datetime(_XNYS_SESSION_CADENCE_TIMESTAMPS[actual_timeframe], utc=True),
            ],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe=configured_timeframe,
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )
        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())

        if configured_timeframe == actual_timeframe:
            backtest.run()
            assert backtest._timeframe == configured_timeframe
        else:
            with pytest.raises(ValueError, match=r"config\.timeframe="):
                backtest.run()

    @pytest.mark.parametrize("timeframe", ["D5", "D10", "D21", "W5"])
    def test_sparse_session_cadence_is_not_promoted_to_a_coarser_unit(
        self,
        timeframe: str,
    ) -> None:
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [
                ["MU"] * 5,
                pd.to_datetime(_XNYS_SESSION_CADENCE_TIMESTAMPS[timeframe], utc=True),
            ],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe=timeframe,
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == timeframe

    @pytest.mark.parametrize("timeframe", ["D5", "W5"])
    def test_long_sparse_session_cadence_stays_stable_across_calendar_boundaries(
        self,
        timeframe: str,
    ) -> None:
        opens = _xnys_session_opens("2024-01-02", "2027-12-31")
        if timeframe == "D5":
            timestamps = opens[2::5][:25]
        else:
            weekly = _first_observation_per_period(opens, "W-SUN")
            timestamps = weekly[1::5][:15]
        frame = _frame_at_timestamps("MU", timestamps)
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe=timeframe,
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == timeframe

    @pytest.mark.parametrize(
        ("timeframe", "phase"),
        [
            *[("D10", phase) for phase in range(10)],
            *[("D21", phase) for phase in range(21)],
        ],
    )
    def test_long_sparse_daily_cadence_preserves_every_phase(
        self,
        timeframe: str,
        phase: int,
    ) -> None:
        interval = int(timeframe[1:])
        opens = _xnys_session_opens("2007-01-03", "2026-12-31")
        frame = _frame_at_timestamps("MU", opens[phase::interval])
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe=timeframe,
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == timeframe

    def test_late_daily_to_weekly_cadence_shift_is_rejected(self) -> None:
        opens = _xnys_session_opens("2026-01-02", "2026-06-30")
        daily = opens[:20]
        weekly = _first_observation_per_period(opens[opens > daily[-1]], "W-SUN")[:5]
        frame = _frame_at_timestamps("MU", daily.append(weekly))
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"symbol 'MU'.*cadence changes.*W"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    def test_late_daily_to_monthly_cadence_shift_is_rejected(self) -> None:
        opens = _xnys_session_opens("2026-01-02", "2026-09-30")
        daily = opens[:20]
        month_ordinals = pd.PeriodIndex(opens.tz_convert(None), freq="M")
        first_later_month = month_ordinals > pd.Period(daily[-1].tz_convert(None), freq="M")
        monthly = _first_observation_per_period(opens[first_later_month], "M")[:5]
        frame = _frame_at_timestamps("MU", daily.append(monthly))
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"symbol 'MU'.*cadence changes.*MN"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    def test_late_weekly_to_monthly_cadence_shift_is_rejected(self) -> None:
        opens = _xnys_session_opens("2025-01-06", "2027-12-31")
        weekly = _first_observation_per_period(opens, "W-SUN")[:20]
        month_ordinals = pd.PeriodIndex(opens.tz_convert(None), freq="M")
        first_later_month = month_ordinals > pd.Period(weekly[-1].tz_convert(None), freq="M")
        monthly = _first_observation_per_period(opens[first_later_month], "M")[:5]
        frame = _frame_at_timestamps("MU", weekly.append(monthly))
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="W1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"symbol 'MU'.*cadence changes.*MN"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    def test_temporary_weekly_like_gap_then_daily_recovery_is_not_drift(self) -> None:
        opens = _xnys_session_opens("2026-01-02", "2026-09-30")
        daily_prefix = opens[:20]
        weekly_like = _first_observation_per_period(
            opens[opens > daily_prefix[-1]],
            "W-SUN",
        )[:5]
        daily_recovery = opens[opens > weekly_like[-1]][:10]
        timestamps = daily_prefix.append(weekly_like).append(daily_recovery)
        frame = _frame_at_timestamps("MU", timestamps)
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        backtest = Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost())
        backtest.run()

        assert backtest._timeframe == "D1"

    @pytest.mark.parametrize(
        ("configured_timeframe", "actual_timeframe"),
        [
            ("D1", "D1"),
            ("W1", "W1"),
            ("MN1", "MN1"),
            ("D1", "MN1"),
        ],
    )
    def test_short_session_sample_fails_closed(
        self,
        configured_timeframe: str,
        actual_timeframe: str,
    ) -> None:
        timestamps = pd.to_datetime(
            _XNYS_SESSION_CADENCE_TIMESTAMPS[actual_timeframe][:4],
            utc=True,
        )
        frame = _frame_at_timestamps("MU", timestamps)
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe=configured_timeframe,
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"at least five session bars.*'MU': 4"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    def test_multi_asset_late_cadence_shift_names_the_affected_symbol(self) -> None:
        opens = _xnys_session_opens("2026-01-02", "2026-06-30")
        daily = opens[:25]
        shifted_prefix = opens[:20]
        shifted_suffix = _first_observation_per_period(
            opens[opens > shifted_prefix[-1]],
            "W-SUN",
        )[:5]
        aaa = _frame_at_timestamps("AAA", daily)
        bbb = _frame_at_timestamps("BBB", shifted_prefix.append(shifted_suffix), price=200.0)
        instrument = {
            "data_adapter": "ibkr",
            "instrument_type": "spot",
            "currency": "USD",
            "security_type": "STK",
        }
        config = make_test_cfg(
            mode="backtest",
            symbols=["AAA", "BBB"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            calendar_id="XNYS",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={"AAA": instrument, "BBB": instrument},
            symbol_cost_overrides={
                "AAA": {"multiplier": 1.0},
                "BBB": {"multiplier": 1.0},
            },
        )

        with pytest.raises(ValueError, match=r"symbol 'BBB'.*cadence changes.*W"):
            Backtest(pd.concat([aaa, bbb]), HoldStrategy(), config=config).run()

    def test_configured_daily_rejects_coarser_symbol_in_multi_asset_data(self) -> None:
        daily = _make_multiindex_df([100.0] * 5, symbol="AAA")
        daily.index = pd.MultiIndex.from_arrays(
            [
                ["AAA"] * 5,
                pd.to_datetime(_XNYS_SESSION_CADENCE_TIMESTAMPS["D1"], utc=True),
            ],
            names=["symbol", "datetime"],
        )
        weekly = _make_multiindex_df([200.0] * 5, symbol="BBB")
        weekly.index = pd.MultiIndex.from_arrays(
            [
                ["BBB"] * 5,
                pd.to_datetime(_XNYS_SESSION_CADENCE_TIMESTAMPS["W1"], utc=True),
            ],
            names=["symbol", "datetime"],
        )
        instrument = {
            "data_adapter": "ibkr",
            "instrument_type": "spot",
            "currency": "USD",
            "security_type": "STK",
        }
        config = make_test_cfg(
            mode="backtest",
            symbols=["AAA", "BBB"],
            timeframe="D1",
            market="us_equity",
            data_source="ibkr",
            calendar_id="XNYS",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={"AAA": instrument, "BBB": instrument},
            symbol_cost_overrides={
                "AAA": {"multiplier": 1.0},
                "BBB": {"multiplier": 1.0},
            },
        )

        with pytest.raises(ValueError, match=r"'BBB': 'W1'"):
            Backtest(pd.concat([daily, weekly]), HoldStrategy(), config=config).run()

    def test_configured_xnys_monthly_timeframe_rejects_midmonth_anchor(self) -> None:
        timestamps = pd.to_datetime(
            [
                "2026-01-15 14:30Z",
                "2026-02-17 14:30Z",
                "2026-03-16 13:30Z",
                "2026-04-15 13:30Z",
                "2026-05-15 13:30Z",
            ],
            utc=True,
        )
        frame = _make_multiindex_df([100.0] * 5, symbol="MU")
        frame.index = pd.MultiIndex.from_arrays(
            [["MU"] * 5, timestamps],
            names=["symbol", "datetime"],
        )
        config = make_test_cfg(
            mode="backtest",
            symbols=["MU"],
            timeframe="MN1",
            market="us_equity",
            data_source="ibkr",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
        )

        with pytest.raises(ValueError, match=r"config\.timeframe=MN1"):
            Backtest(frame, HoldStrategy(), config=config, cost_model=_zero_cost()).run()

    @pytest.mark.parametrize(
        ("timeframe", "aaa_timestamps", "bbb_timestamps"),
        [
            (
                "D2",
                [
                    "2026-03-02 14:30Z",
                    "2026-03-04 14:30Z",
                    "2026-03-06 14:30Z",
                    "2026-03-10 13:30Z",
                    "2026-03-12 13:30Z",
                ],
                [
                    "2026-03-03 14:30Z",
                    "2026-03-05 14:30Z",
                    "2026-03-09 13:30Z",
                    "2026-03-11 13:30Z",
                    "2026-03-13 13:30Z",
                ],
            ),
            (
                "W2",
                [
                    "2026-03-02 14:30Z",
                    "2026-03-16 13:30Z",
                    "2026-03-30 13:30Z",
                    "2026-04-13 13:30Z",
                    "2026-04-27 13:30Z",
                ],
                [
                    "2026-03-09 13:30Z",
                    "2026-03-23 13:30Z",
                    "2026-04-06 13:30Z",
                    "2026-04-20 13:30Z",
                    "2026-05-04 13:30Z",
                ],
            ),
        ],
    )
    def test_multi_period_calendar_cadence_has_no_cross_symbol_global_phase(
        self,
        timeframe: str,
        aaa_timestamps: list[str],
        bbb_timestamps: list[str],
    ) -> None:
        aaa = _make_multiindex_df([100.0] * 5, symbol="AAA")
        aaa.index = pd.MultiIndex.from_arrays(
            [["AAA"] * 5, pd.to_datetime(aaa_timestamps, utc=True)],
            names=["symbol", "datetime"],
        )
        bbb = _make_multiindex_df([200.0] * 5, symbol="BBB")
        bbb.index = pd.MultiIndex.from_arrays(
            [["BBB"] * 5, pd.to_datetime(bbb_timestamps, utc=True)],
            names=["symbol", "datetime"],
        )
        instrument = {
            "data_adapter": "ibkr",
            "instrument_type": "spot",
            "currency": "USD",
            "security_type": "STK",
        }
        config = make_test_cfg(
            mode="backtest",
            symbols=["AAA", "BBB"],
            timeframe=timeframe,
            market="us_equity",
            data_source="ibkr",
            calendar_id="XNYS",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            instrument_overrides={"AAA": instrument, "BBB": instrument},
            symbol_cost_overrides={
                "AAA": {"multiplier": 1.0},
                "BBB": {"multiplier": 1.0},
            },
        )

        backtest = Backtest(pd.concat([aaa, bbb]), HoldStrategy(), config=config)
        backtest.run()

        assert backtest._timeframe == timeframe


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
                close = base_price + i
                rows.append(
                    {
                        "symbol": sym,
                        "datetime": dt[i],
                        "open": base_price,
                        "high": close * 1.001,
                        "low": base_price * 0.999,
                        "close": close,  # trending up
                        "volume": 100.0,
                    }
                )

        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class BuyBothBar2(Strategy):
            def on_bar(self, ctx):
                actions = []
                if ctx.period_index == 2:
                    n_symbols = len(ctx.symbols)
                    for sym in ctx.symbols:
                        if sym not in ctx.positions:
                            bar = ctx.bars.get(sym, {})
                            price = bar.get("close", 1.0)
                            qty = (ctx.cash / n_symbols) / price if price > 0 else 0
                            actions.append(OrderIntent(action="long", symbol=sym, quantity=qty))
                if ctx.period_index == 5:
                    for sym in ctx.symbols:
                        if sym in ctx.positions:
                            actions.append(OrderIntent(action="close", symbol=sym))
                return actions

        bt = Backtest(
            df, BuyBothBar2(), initial_balance=100_000, cost_model=_zero_cost(), data_source="test"
        )
        result = bt.run()

        assert len(result.trades) == 2
        symbols_traded = {t.symbol for t in result.trades}
        assert symbols_traded == {"AAA", "BBB"}

    def test_symbol_start_date_does_not_delay_available_universe(self) -> None:
        timeline = pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC")
        rows = []
        for i, ts in enumerate(timeline):
            symbols = ("AAA",) if i < 2 else ("AAA", "BBB")
            for symbol in symbols:
                price = 100.0 if symbol == "AAA" else 200.0
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])
        available: list[tuple[str, ...]] = []

        class ObserveUniverse(Strategy):
            def on_bar(self, ctx):
                available.append(ctx.available_symbols)
                return []

        Backtest(
            df,
            ObserveUniverse(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        assert available == [
            ("AAA",),
            ("AAA",),
            ("AAA", "BBB"),
            ("AAA", "BBB"),
            ("AAA", "BBB"),
        ]

    def test_unready_grouped_decision_does_not_block_an_unrelated_symbol(self) -> None:
        """NEXT joins the universe two periods late. A strategy that
        self-checks readiness via ctx.available_symbols before proposing a
        group_id-tagged NEAR/NEXT pair must still be able to trade SOLO in the
        meantime — the engine no longer queues an incomplete grouped
        decision and blocks on_bar for everything else while it waits."""
        timeline = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
        rows = []
        for i, ts in enumerate(timeline):
            symbols = ("NEAR", "SOLO") if i < 2 else ("NEAR", "NEXT", "SOLO")
            for symbol in symbols:
                price = {"NEAR": 100.0, "NEXT": 50.0, "SOLO": 200.0}[symbol]
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class SpreadPlusSolo(Strategy):
            def on_bar(self, ctx):
                if {"NEAR", "NEXT"}.issubset(ctx.available_symbols) and "NEAR" not in ctx.positions:
                    return [
                        OrderIntent(action="long", symbol="NEAR", quantity=1.0, group_id="spread"),
                        OrderIntent(action="short", symbol="NEXT", quantity=1.0, group_id="spread"),
                    ]
                if "SOLO" in ctx.available_symbols and "SOLO" not in ctx.positions:
                    return [OrderIntent(action="long", symbol="SOLO", quantity=1.0)]
                return []

        result = Backtest(
            df,
            SpreadPlusSolo(),
            initial_balance=100_000,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        symbols_traded = {t.symbol for t in result.trades}
        assert symbols_traded == {"NEAR", "NEXT", "SOLO"}

    def test_two_concurrent_groups_execute_independently_in_one_decision(self) -> None:
        """A single on_bar return can carry multiple independent arbitrage
        groups at once — this is the core scenario group_id exists for.
        Each group fills atomically and its PositionEvents/TradeResults carry
        its own group_id; the two groups never interact."""
        timeline = pd.date_range("2025-01-01", periods=6, freq="h", tz="UTC")
        rows = []
        for ts in timeline:
            for symbol, price in [("A", 100.0), ("B", 100.0), ("C", 50.0), ("D", 50.0)]:
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class TwoSpreads(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [
                        OrderIntent(action="long", symbol="A", quantity=1.0, group_id="pair_ab"),
                        OrderIntent(action="short", symbol="B", quantity=1.0, group_id="pair_ab"),
                        OrderIntent(action="long", symbol="C", quantity=2.0, group_id="pair_cd"),
                        OrderIntent(action="short", symbol="D", quantity=2.0, group_id="pair_cd"),
                    ]
                return []

        result = Backtest(
            df,
            TwoSpreads(),
            initial_balance=100_000,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        events_by_symbol = {e.symbol: e for e in result.position_events if e.event_type == "open"}
        assert events_by_symbol["A"].group_id == "pair_ab"
        assert events_by_symbol["B"].group_id == "pair_ab"
        assert events_by_symbol["C"].group_id == "pair_cd"
        assert events_by_symbol["D"].group_id == "pair_cd"

    def test_time_in_force_flows_from_order_intent_to_order_event(self) -> None:
        """time_in_force is a live-only hint that backtest ignores for fill
        logic, but it must still round-trip onto the resulting PositionEvent so
        it's visible in output regardless of run mode."""
        df = _make_multiindex_df([100.0] * 5)

        class BuyWithIoc(Strategy):
            def on_bar(self, ctx: Context) -> list[OrderIntent]:
                if ctx.period_index == 0:
                    return [
                        OrderIntent(
                            action="long", symbol=ctx.symbol, quantity=1.0, time_in_force="ioc"
                        )
                    ]
                return []

        result = Backtest(
            df,
            BuyWithIoc(),
            initial_balance=100_000,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        open_event = next(e for e in result.position_events if e.event_type == "open")
        assert open_event.time_in_force == "ioc"

    def test_partial_bars_run_strategy_without_consuming_other_symbol_intent(self) -> None:
        timeline = pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC")
        rows = []
        for i, ts in enumerate(timeline):
            for symbol in ["AAA"] if i == 1 else ["AAA", "BBB"]:
                price = 100.0 + i if symbol == "AAA" else 200.0 + 10 * i
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        seen_cycles: list[tuple[pd.Timestamp, int, set[str]]] = []

        class BuyBbbOnFirstCycle(Strategy):
            def on_bar(self, ctx):
                seen_cycles.append((ctx.ts, ctx.period_index, set(ctx.bars)))
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="BBB", quantity=1.0)]
                return []

        result = Backtest(
            df,
            BuyBbbOnFirstCycle(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        open_event = next(event for event in result.position_events if event.event_type == "open")
        assert open_event.ts == timeline[2]
        assert open_event.price == pytest.approx(220.0)
        assert [cycle[0] for cycle in seen_cycles] == [
            timeline[0],
            timeline[1],
            timeline[2],
            timeline[3],
            timeline[4],
        ]
        assert [cycle[1] for cycle in seen_cycles] == [0, 1, 2, 3, 4]
        assert [cycle[2] for cycle in seen_cycles] == [
            {"AAA", "BBB"},
            {"AAA"},
            {"AAA", "BBB"},
            {"AAA", "BBB"},
            {"AAA", "BBB"},
        ]

    def test_missing_bar_uses_last_mark_and_does_not_age_position(self) -> None:
        timeline = pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC")
        aaa_prices = {0: 100.0, 1: 120.0, 3: 130.0, 4: 140.0}
        rows = []
        for i, ts in enumerate(timeline):
            for symbol in ("AAA", "BBB"):
                if symbol == "AAA" and i not in aaa_prices:
                    continue
                price = aaa_prices[i] if symbol == "AAA" else 200.0
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": 100.0 if symbol == "AAA" and i == 1 else price,
                        "high": price,
                        "low": min(price, 100.0) if symbol == "AAA" else price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class BuyAaaOnFirstCycle(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="AAA", quantity=1.0)]
                return []

        result = Backtest(
            df,
            BuyAaaOnFirstCycle(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        ).run()

        assert result.equity_curve[2].equity == pytest.approx(1_020.0)
        assert result.equity_curve[4].equity == pytest.approx(1_040.0)
        assert result.trades[0].exit_price == pytest.approx(140.0)
        assert result.trades[0].periods_held == 3

    def test_end_of_run_does_not_invent_liquidation_for_missing_symbol(self) -> None:
        timeline = pd.date_range("2025-01-01", periods=5, freq="h", tz="UTC")
        rows = []
        for i, ts in enumerate(timeline):
            for symbol in ("AAA", "BBB"):
                if symbol == "AAA" and i == 4:
                    continue
                price = 100.0 if symbol == "AAA" else 200.0
                rows.append(
                    {
                        "symbol": symbol,
                        "datetime": ts,
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": 100.0,
                    }
                )
        df = pd.DataFrame(rows).set_index(["symbol", "datetime"])

        class BuyAaa(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="AAA", quantity=1.0)]
                return []

        backtest = Backtest(
            df,
            BuyAaa(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        )
        result = backtest.run()

        assert not [trade for trade in result.trades if trade.symbol == "AAA"]
        skips = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "force_close_incomplete"
        ]
        assert [event.symbol for event in skips] == ["AAA"]
        assert skips[0].detail["remaining_quantity"] == 1.0
        _assert_terminal_equity_reconciles(backtest, result, 1_000.0)

    def test_end_of_run_does_not_invent_liquidity_for_untradable_exit(self) -> None:
        df = _make_multiindex_df([100.0] * 5, symbol="AAA")
        df["can_buy"] = True
        df["can_sell"] = True
        df.loc[("AAA", df.index.get_level_values("datetime")[-1]), "can_sell"] = False

        class BuyAaa(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="AAA", quantity=1.0)]
                return []

        backtest = Backtest(
            df,
            BuyAaa(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
        )
        result = backtest.run()

        assert not [trade for trade in result.trades if trade.symbol == "AAA"]
        skips = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "force_close_incomplete"
        ]
        assert [event.symbol for event in skips] == ["AAA"]
        # Equity still marks the position that could not be sold.
        _assert_terminal_equity_reconciles(backtest, result, 1_000.0)

    def test_end_of_run_partially_closes_under_the_volume_cap(self) -> None:
        """A thin final bar closes what it can and reports the rest."""
        df = _make_multiindex_df([100.0] * 5, symbol="AAA")
        # Thick bars while trading, thin only at the end: the entry must not be
        # capped too, or nothing is left over to report.
        df["volume"] = 1_000.0
        df.loc[("AAA", df.index.get_level_values("datetime")[-1]), "volume"] = 10.0

        class BuyAaa(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 0:
                    return [OrderIntent(action="long", symbol="AAA", quantity=5.0)]
                return []

        backtest = Backtest(
            df,
            BuyAaa(),
            initial_balance=1_000.0,
            cost_model=_zero_cost(),
            data_source="test",
            execution=ExecutionPolicy(max_bar_volume_participation_rate=0.1),
        )
        result = backtest.run()

        # 10% of a 10-unit bar sells 1 of the 5 units held, so the forced exit
        # lands as a partial reduce rather than a close.
        exits = [event for event in result.position_events if event.event_type == "reduce"]
        assert [event.fill_quantity for event in exits] == [pytest.approx(1.0)]
        skips = [
            event
            for event in result.runtime_events
            if event.detail.get("reason") == "force_close_incomplete"
        ]
        assert [event.symbol for event in skips] == ["AAA"]
        assert skips[0].detail["remaining_quantity"] == pytest.approx(4.0)
        _assert_terminal_equity_reconciles(backtest, result, 1_000.0)

    def test_per_symbol_multiplier_resolved_independently_via_cfg(self) -> None:
        """Regression: a multi-asset config= run used to build exactly one
        CostModel from cfg.symbol (symbols[0]) and apply it to every symbol
        — TXFR1 (multiplier=200) and MXFR1 (multiplier=50) in the same
        tw_futures run would have silently shared TXFR1's multiplier."""
        from librae.core.run_config import AccountConfig, RunConfig

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
            account=AccountConfig(currency="TWD", initial_cash=100_000.0),
            mode="backtest",
        )
        bt = Backtest(data=df, strategy=HoldStrategy(), config=cfg)
        assert bt._get_cost_model("TXFR1").multiplier == 200.0
        assert bt._get_cost_model("MXFR1").multiplier == 50.0

    def test_per_symbol_market_costs_resolved_independently_via_cfg(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        df = pd.concat(
            [
                _make_multiindex_df([100.0] * 5, symbol="CRYPTO"),
                _make_multiindex_df([100.0] * 5, symbol="MU"),
            ]
        )
        cfg = RunConfig(
            strategy_name="t",
            symbols=["CRYPTO", "MU"],
            timeframe="1d",
            market="multi",
            data_source="multi",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            mode="backtest",
            symbol_cost_overrides={"CRYPTO": {"multiplier": 1.0}},
            instrument_overrides={
                "CRYPTO": {
                    "market": "crypto",
                    "data_source": "binance_spot",
                    "data_adapter": "crypto",
                    "instrument_type": "spot",
                    "currency": "USD",
                },
            },
        )

        bt = Backtest(data=df, strategy=HoldStrategy(), config=cfg)

        assert bt._get_cost_model("CRYPTO").commission_rate == 0.001
        assert bt._get_cost_model("MU").min_commission == 0.0
        assert bt._get_cost_model("MU").short_margin_rate == 0.5

    def test_per_symbol_multiplier_via_symbol_cost_overrides_no_yaml_edit_needed(self) -> None:
        """An unregistered symbol works via cfg.symbol_cost_overrides alone —
        no symbols.py registry entry required."""
        from librae.core.run_config import AccountConfig, RunConfig

        df = _make_multiindex_df([1.0] * 5, symbol="MY_CUSTOM_SYMBOL")
        cfg = RunConfig(
            strategy_name="t",
            symbols=["MY_CUSTOM_SYMBOL"],
            timeframe="1h",
            market="crypto",
            data_source="x",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            mode="backtest",
            symbol_cost_overrides={"MY_CUSTOM_SYMBOL": {"multiplier": 1.0}},
            instrument_overrides={
                "MY_CUSTOM_SYMBOL": {
                    "instrument_type": "spot",
                    "currency": "USD",
                    "data_adapter": "crypto",
                }
            },
        )
        bt = Backtest(data=df, strategy=HoldStrategy(), config=cfg)
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

        class Spy(Strategy):
            def on_bar(self, ctx):
                seen_positions.append(dict(ctx.positions))
                if ctx.period_index == 1:
                    return [OrderIntent(action="long", symbol=ctx.symbol)]
                if ctx.period_index == 4:
                    return [OrderIntent(action="close", symbol=ctx.symbol)]
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

        class Tracker(Strategy):
            def on_bar(self, ctx):
                pos = ctx.positions.get(ctx.symbol)
                if pos:
                    held_values.append(pos.periods_held)
                if ctx.period_index == 1:
                    return [OrderIntent(action="long", symbol=ctx.symbol)]
                if ctx.period_index == 5:
                    return [OrderIntent(action="close", symbol=ctx.symbol)]
                return []

        df = _make_multiindex_df([100.0] * 8)
        bt = Backtest(
            df, Tracker(), initial_balance=10_000, cost_model=_zero_cost(), data_source="test"
        )
        bt.run()

        # WHY: next-bar execution — buy queued at bar 1, fills at bar 2.
        # Bar 2 sees periods_held=0 (just entered), then increments each bar.
        assert held_values == [0, 1, 2, 3]
