"""Tests for the Backtest engine: Strategy protocol + Executor pattern."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.run_config import ExecutionPolicy, RiskPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from tests.conftest import make_test_cfg

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

        open_event = next(event for event in result.order_events if event.event_type == "open")
        assert open_event.fill_quantity == pytest.approx(10.0)

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

        open_event = next(event for event in result.order_events if event.event_type == "open")
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
            event for event in result.order_events if event.event_type in ("open", "add")
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

    def test_cfg_symbols_must_match_data(self) -> None:
        df = _make_multiindex_df([100.0] * 5)
        cfg = make_test_cfg(mode="backtest", symbols=["ETHUSDT"])

        with pytest.raises(ValueError, match="must exactly match data symbols"):
            Backtest(df, HoldStrategy(), config=cfg, cost_model=_zero_cost())


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

        open_event = next(event for event in result.order_events if event.event_type == "open")
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

        with pytest.raises(ValueError, match=r"cannot force-close.*AAA"):
            Backtest(
                df,
                BuyAaa(),
                initial_balance=1_000.0,
                cost_model=_zero_cost(),
                data_source="test",
            ).run()

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
            accounts={"default": AccountConfig(currency="TWD", initial_cash=100_000.0)},
            mode="backtest",
        )
        bt = Backtest(data=df, strategy=HoldStrategy(), config=cfg)
        assert bt._get_cost_model("TXFR1").multiplier == 200.0
        assert bt._get_cost_model("MXFR1").multiplier == 50.0

    def test_per_symbol_market_costs_resolved_independently_via_cfg(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        df = pd.concat(
            [
                _make_multiindex_df([100.0] * 5, symbol="TXFR1"),
                _make_multiindex_df([100.0] * 5, symbol="MU"),
            ]
        )
        cfg = RunConfig(
            strategy_name="t",
            symbols=["TXFR1", "MU"],
            timeframe="1d",
            market="multi",
            data_source="multi",
            accounts={
                "futures": AccountConfig(currency="TWD", initial_cash=100_000.0),
                "equity": AccountConfig(currency="USD", initial_cash=100_000.0),
            },
            mode="backtest",
            instrument_overrides={
                "TXFR1": {"account_id": "futures"},
                "MU": {"account_id": "equity"},
            },
        )

        bt = Backtest(data=df, strategy=HoldStrategy(), config=cfg)

        assert bt._get_cost_model("TXFR1").min_commission == 100.0
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
            accounts={"default": AccountConfig(currency="USD", initial_cash=100_000.0)},
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
