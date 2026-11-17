"""Tests for the metrics module.

Tests compute_all() which accepts primitive sequences (equity values,
timestamps, closed trades).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.backtest.schema import StrategyMetrics
from librae.core.cost_model import CostModel
from librae.core.metrics import (
    DEFAULT_SERIES_METRICS,
    DEFAULT_SUMMARY_METRICS,
    PERCENTAGE_POINTS_PER_FRACTION,
    available_metrics,
    closed_trades,
    compute_all,
    compute_performance_series,
    compute_signal_outcomes,
    compute_trade_entry_outcomes,
    summarize_performance,
)
from librae.core.strategy import OrderIntent, Strategy

from tests.signal_outcome_contract import (
    SIGNAL_OUTCOME_LONG_FRACTIONS,
    SIGNAL_OUTCOME_LONG_PERCENTAGE_POINTS,
    make_signal_outcome_contract_ohlcv,
)

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
END = datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC)


def test_signal_outcome_functions_are_public() -> None:
    import librae

    assert librae.compute_signal_outcomes is compute_signal_outcomes


def test_performance_functions_are_public() -> None:
    import librae

    assert librae.available_metrics is available_metrics
    assert librae.summarize_performance is summarize_performance
    assert librae.compute_performance_series is compute_performance_series


def test_available_metrics_lists_supported_names_by_kind() -> None:
    summary = available_metrics(kind="summary")
    series = available_metrics(kind="series")

    assert set(DEFAULT_SUMMARY_METRICS) <= set(summary)
    assert set(DEFAULT_SERIES_METRICS) <= set(series)
    assert available_metrics() == (*summary, *series)


def test_available_metrics_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unsupported metric kind"):
        available_metrics(kind="report")


@pytest.fixture
def period_returns() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "strategy_a": [0.10, -0.05, 0.02],
            "strategy_b": [0.00, 0.03, -0.01],
        },
        index=pd.date_range(START, periods=3, freq="D", tz="UTC"),
    )


class TestPerformanceAnalysis:
    def test_summary_preserves_requested_order(self, period_returns: pd.DataFrame) -> None:
        result = summarize_performance(
            period_returns[["strategy_b", "strategy_a"]],
            metrics=("max_drawdown", "total_return", "mean_period_return"),
        )

        assert result.index.name == "metric"
        assert result.index.tolist() == [
            "max_drawdown",
            "total_return",
            "mean_period_return",
        ]
        assert result.columns.tolist() == ["strategy_b", "strategy_a"]
        assert result.loc["total_return", "strategy_a"] == pytest.approx((1.10 * 0.95 * 1.02) - 1.0)

    def test_series_returns_requested_paths(self, period_returns: pd.DataFrame) -> None:
        result = compute_performance_series(
            period_returns,
            metrics=("cumulative_return", "drawdown"),
        )

        assert list(result) == ["cumulative_return", "drawdown"]
        assert result["cumulative_return"].index.equals(period_returns.index)
        assert result["cumulative_return"].columns.equals(period_returns.columns)
        assert result["cumulative_return"].iloc[-1, 0] == pytest.approx((1.10 * 0.95 * 1.02) - 1.0)

    def test_first_period_loss_is_a_drawdown(self) -> None:
        returns = pd.DataFrame(
            {"strategy": [-0.10, 0.05]},
            index=pd.date_range(START, periods=2, freq="D", tz="UTC"),
        )

        drawdown = compute_performance_series(returns, metrics=("drawdown",))["drawdown"]

        assert drawdown.iloc[0, 0] == pytest.approx(-0.10)

    def test_target_return_applies_to_period_ratios(self, period_returns: pd.DataFrame) -> None:
        result = summarize_performance(
            period_returns[["strategy_a"]],
            metrics=("period_sharpe", "period_sortino"),
            period_target_return=0.01,
        )
        excess = period_returns["strategy_a"] - 0.01
        downside = np.sqrt(np.mean(np.minimum(excess, 0.0) ** 2))

        assert result.loc["period_sharpe", "strategy_a"] == pytest.approx(
            excess.mean() / excess.std(ddof=1)
        )
        assert result.loc["period_sortino", "strategy_a"] == pytest.approx(excess.mean() / downside)

    def test_active_returns_are_composed_by_the_caller(self, period_returns: pd.DataFrame) -> None:
        active = pd.DataFrame(
            {"active": period_returns["strategy_a"] - period_returns["strategy_b"]}
        )

        result = summarize_performance(
            active,
            metrics=("period_volatility", "period_sharpe"),
        )

        assert result.loc["period_volatility", "active"] == pytest.approx(
            active["active"].std(ddof=1)
        )
        assert result.loc["period_sharpe", "active"] == pytest.approx(
            active["active"].mean() / active["active"].std(ddof=1)
        )

    @pytest.mark.parametrize(
        ("mutate", "message"),
        [
            (lambda frame: frame.set_axis(frame.index.tz_localize(None)), "timezone-aware"),
            (lambda frame: frame.iloc[::-1], "sorted"),
            (
                lambda frame: frame.set_axis(
                    pd.DatetimeIndex([frame.index[0], frame.index[0], frame.index[2]])
                ),
                "unique",
            ),
            (lambda frame: frame.rename(columns={"strategy_b": "strategy_a"}), "unique"),
            (lambda frame: frame.rename(columns={"strategy_a": ""}), "non-empty strings"),
            (lambda frame: frame.assign(strategy_a=np.nan), "finite"),
            (lambda frame: frame.assign(strategy_a=-1.0), "greater than -1"),
        ],
    )
    def test_period_return_contract_is_strict(
        self,
        period_returns: pd.DataFrame,
        mutate,
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            summarize_performance(mutate(period_returns))

    @pytest.mark.parametrize(
        ("metrics", "error", "message"),
        [
            ("total_return", TypeError, "sequence"),
            ((), ValueError, "at least one"),
            (("total_return", "total_return"), ValueError, "duplicates"),
            (("unknown",), ValueError, "unsupported"),
        ],
    )
    def test_metric_selection_is_validated(
        self,
        period_returns: pd.DataFrame,
        metrics,
        error: type[Exception],
        message: str,
    ) -> None:
        with pytest.raises(error, match=message):
            summarize_performance(period_returns, metrics=metrics)


def _trade(
    net_pnl: float = 0.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    net_return: float = 0.0,
    entry_notional: float = 100.0,
) -> dict[str, float]:
    """The closed_trades() columns compute_all reads."""
    return {
        "net_pnl": net_pnl,
        "commission": commission,
        "slippage": slippage,
        "tax": tax,
        "entry_notional": entry_notional,
        "net_return": net_return,
    }


def _trades(*rows: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(_trade()))


def _call_compute_all(
    equity: list[float],
    trades: list[dict[str, float]] | None = None,
    exposed_periods: int | None = None,
) -> StrategyMetrics:
    """Helper to call compute_all with timestamps derived from equity length."""
    timestamps = pd.date_range(START, periods=len(equity), freq="h", tz="UTC").tolist()
    return compute_all(
        equity_values=equity,
        timestamps=timestamps,
        trades=_trades(*(trades or [])),
        total_periods=len(equity),
        exposed_periods=exposed_periods,
    )


class TestComputeAllEmpty:
    def test_no_equity(self) -> None:
        m = _call_compute_all([])
        assert m.trades == 0
        assert np.isclose(m.total_return, 0.0)

    def test_no_trades(self) -> None:
        m = _call_compute_all([10_000.0] * 10)
        assert m.trades == 0

    def test_no_trades_keeps_period_metrics(self) -> None:
        equity = np.array([10_000.0, 10_500.0, 9_500.0, 10_000.0])
        timestamps = pd.date_range(START, periods=len(equity), freq="h", tz="UTC").tolist()

        m = compute_all(
            equity_values=equity,
            timestamps=timestamps,
            trades=_trades(),
            total_periods=len(equity),
        )

        assert m.trades == 0
        assert m.max_drawdown < 0
        assert m.period_volatility is not None


class TestComputeAllValidation:
    @pytest.mark.parametrize("invalid_equity", [0.0, -1.0, np.nan, np.inf])
    def test_equity_must_be_finite_and_positive(self, invalid_equity: float) -> None:
        with pytest.raises(ValueError, match="equity_values"):
            _call_compute_all([100.0, invalid_equity])

    def test_equity_and_timestamp_lengths_must_match(self) -> None:
        with pytest.raises(ValueError, match="timestamps length"):
            compute_all(
                equity_values=[100.0, 101.0],
                timestamps=[START],
                trades=_trades(),
                total_periods=2,
            )

    @pytest.mark.parametrize("period_target_return", [np.nan, np.inf, True])
    def test_period_target_return_is_validated(self, period_target_return: float) -> None:
        timestamps = pd.date_range(START, periods=2, freq="h", tz="UTC").tolist()

        with pytest.raises(ValueError, match="period_target_return"):
            compute_all(
                equity_values=[100.0, 101.0],
                timestamps=timestamps,
                trades=_trades(),
                total_periods=2,
                period_target_return=period_target_return,
            )


class TestComputeAllMetrics:
    def test_period_sharpe_is_not_annualized(self) -> None:
        timestamps = pd.date_range(START, periods=3, freq="h", tz="UTC").tolist()

        metrics = compute_all(
            equity_values=[100.0, 101.0, 100.5],
            timestamps=timestamps,
            trades=_trades(),
            total_periods=3,
        )

        returns = np.array([0.01, 100.5 / 101.0 - 1.0])
        expected = np.mean(returns) / np.std(returns, ddof=1)
        assert metrics.period_sharpe == pytest.approx(expected)

    def test_positive_return(self) -> None:
        pnl = _trade(net_pnl=100, net_return=1.0)
        m = _call_compute_all([10_000.0, 10_000.0, 10_100.0], [pnl])
        assert m.trades == 1
        assert m.total_return > 0

    def test_win_rate(self) -> None:
        pnls = [
            _trade(net_pnl=10, net_return=0.1),  # win
            _trade(net_pnl=-10, net_return=-0.1),  # loss
            _trade(net_pnl=5, net_return=0.05),  # win
        ]
        m = _call_compute_all([10_000.0, 10_100.0, 9_900.0, 10_050.0], pnls)
        assert np.isclose(m.win_rate, 2 / 3)

    def test_profit_factor(self) -> None:
        pnls = [
            _trade(net_pnl=20, net_return=0.2),
            _trade(net_pnl=-10, net_return=-0.1),
        ]
        m = _call_compute_all([10_000.0, 10_200.0, 10_100.0], pnls)
        assert m.profit_factor == 2.0

    def test_profit_factor_is_none_without_losses(self) -> None:
        pnls = [
            _trade(net_pnl=20, net_return=0.2),
            _trade(net_pnl=10, net_return=0.1),
        ]
        m = _call_compute_all([10_000.0, 10_020.0, 10_030.0], pnls)
        assert m.profit_factor is None

    def test_exposure_ratio(self) -> None:
        pnl = _trade(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0] * 20, [pnl], exposed_periods=5)
        assert np.isclose(m.exposure_ratio, 5 / 20)

    def test_cost_totals(self) -> None:
        trade = _trade(net_pnl=6.5, commission=2.0, slippage=1.0, tax=0.5, net_return=0.065)
        m = _call_compute_all([10_000.0, 10_003.0, 10_006.5], [trade])
        assert np.isclose(m.total_commission, 2.0)
        assert np.isclose(m.total_slippage, 1.0)

    def test_avg_trade_return_uses_notional_weights(self) -> None:
        trades = [
            _trade(net_pnl=10.0, net_return=0.10, entry_notional=100.0),
            _trade(net_pnl=9.0, net_return=0.01, entry_notional=900.0),
        ]

        metrics = _call_compute_all([100.0, 110.0, 119.0], trades)

        assert metrics.avg_trade_return == pytest.approx(0.019)

    @pytest.mark.parametrize("invalid_notional", [0.0, -1.0, np.nan, np.inf])
    def test_trade_notional_weights_must_be_finite_and_positive(
        self, invalid_notional: float
    ) -> None:
        with pytest.raises(ValueError, match="entry_notional"):
            _call_compute_all(
                [100.0, 101.0],
                [_trade(net_pnl=1.0, net_return=0.01, entry_notional=invalid_notional)],
            )

    def test_trades_must_carry_the_closed_trade_columns(self) -> None:
        timestamps = pd.date_range(START, periods=2, freq="h", tz="UTC").tolist()

        with pytest.raises(ValueError, match="entry_notional"):
            compute_all(
                equity_values=[100.0, 101.0],
                timestamps=timestamps,
                trades=pd.DataFrame({"net_pnl": [1.0]}),
                total_periods=2,
            )

    def test_portfolio_diagnostics(self) -> None:
        timestamps = pd.date_range(START, periods=3, freq="h", tz="UTC").tolist()
        m = compute_all(
            equity_values=[100.0, 101.0, 102.0],
            timestamps=timestamps,
            trades=_trades(),
            total_periods=3,
            turnover_values=[0.2, 0.1, 0.3],
            gross_exposure_values=[0.5, 1.2, 0.8],
            net_exposure_values=[0.5, -0.4, 0.2],
            concentration_values=[0.5, 0.7, 0.4],
        )

        assert m.total_turnover == pytest.approx(0.6)
        assert m.average_gross_exposure == pytest.approx(2.5 / 3)
        assert m.max_gross_exposure == pytest.approx(1.2)
        assert m.max_abs_net_exposure == pytest.approx(0.5)
        assert m.max_concentration == pytest.approx(0.7)

    def test_period_ratios_are_none_when_denominators_are_zero(self) -> None:
        m = _call_compute_all([10_000.0] * 4)

        assert m.period_sharpe is None
        assert m.period_sortino is None
        assert m.max_drawdown == pytest.approx(0.0)

    def test_max_drawdown_negative(self) -> None:
        pnl = _trade(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0, 10_500.0, 9_800.0, 10_200.0], [pnl])
        assert m.max_drawdown <= 0

    def test_compute_all_uses_the_period_summary_contract(self) -> None:
        timestamps = pd.date_range(START, periods=4, freq="D", tz="UTC").tolist()
        equity = np.array([10_000.0, 10_100.0, 10_050.0, 10_200.0])
        metrics = compute_all(
            equity_values=equity,
            timestamps=timestamps,
            trades=_trades(_trade(net_pnl=200.0)),
            total_periods=4,
            period_target_return=0.001,
        )

        returns = equity[1:] / equity[:-1] - 1.0
        excess = returns - 0.001
        downside = np.sqrt(np.square(excess[excess < 0.0]).sum() / len(excess))
        max_drawdown = np.min(equity / np.maximum.accumulate(equity) - 1.0)

        assert metrics.period_sharpe == pytest.approx(np.mean(excess) / np.std(excess, ddof=1))
        assert metrics.period_sortino == pytest.approx(np.mean(excess) / downside)
        assert metrics.period_volatility == pytest.approx(np.std(returns, ddof=1))
        assert metrics.max_drawdown == pytest.approx(max_drawdown)


FIRST_EXIT = START + pd.Timedelta(hours=2)


def _round_trip(
    symbol: str,
    realized_pnl: float,
    *,
    side: str = "long",
    quantity: float = 1.0,
    entry_price: float = 100.0,
    exit_price: float = 100.0,
    multiplier: float = 1.0,
    entry_at: datetime = START,
    exit_at: datetime = FIRST_EXIT,
    group_id: str | None = None,
) -> list[dict]:
    """An open and its close, with the fields a stored position event has."""
    common = {
        "symbol": symbol,
        "side": side,
        "fill_quantity": quantity,
        "entry_price": entry_price,
        "entry_at": entry_at,
        "group_id": group_id,
        "reason": "",
    }
    return [
        {
            **common,
            "ts": entry_at,
            "event_type": "open",
            "price": entry_price,
            "remaining_quantity": quantity,
            "notional": entry_price * quantity * multiplier,
            "commission": 1.0,
            "slippage": 0.5,
            "tax": 0.0,
            "realized_pnl": None,
            "entry_commission": None,
            "entry_slippage": None,
            "entry_tax": None,
            "periods_held": None,
        },
        {
            **common,
            "ts": exit_at,
            "event_type": "close",
            "price": exit_price,
            "remaining_quantity": 0.0,
            "notional": exit_price * quantity * multiplier,
            "commission": 2.0,
            "slippage": 0.5,
            "tax": 0.25,
            "realized_pnl": realized_pnl,
            "entry_commission": 1.0,
            "entry_slippage": 0.5,
            "entry_tax": 0.0,
            "periods_held": 2,
        },
    ]


def _events(*round_trips: list[dict]) -> pd.DataFrame:
    rows = [row for trip in round_trips for row in trip]
    return pd.DataFrame(rows).sort_values("ts", kind="stable", ignore_index=True)


NO_FINANCING = pd.DataFrame()


class TestClosedTrades:
    def test_an_exit_carries_both_sides_costs_and_its_entry_notional(self) -> None:
        events = _events(
            _round_trip("FUT", 196.25, quantity=2.0, exit_price=110.0, multiplier=10.0)
        )

        trade = closed_trades(events, NO_FINANCING).iloc[0]

        assert (trade["symbol"], trade["side"], trade["exit_at"]) == (
            "FUT",
            "long",
            START + pd.Timedelta(hours=2),
        )
        assert (trade["commission"], trade["slippage"], trade["tax"]) == (3.0, 1.0, 0.25)
        # The stored notional is at the exit price; the entry notional is 2 * 100 * 10.
        assert trade["entry_notional"] == pytest.approx(2_000.0)
        assert trade["net_return"] == pytest.approx(196.25 / 2_000.0)

    def test_hedge_legs_net_into_one_trade(self) -> None:
        """A perp leg alone looks like a big loser and the spot leg a winner;
        together they lost 5 on 600 of notional."""
        events = _events(
            _round_trip("PERP", -30.0, side="short", group_id="hedge-1"),
            _round_trip("SPOT", 25.0, quantity=5.0, group_id="hedge-1"),
        )

        trades = closed_trades(events, NO_FINANCING)

        assert len(trades) == 1
        trade = trades.iloc[0]
        assert (trade["symbol"], trade["side"], trade["group_id"]) == (
            "PERP / SPOT",
            None,
            "hedge-1",
        )
        assert trade["net_pnl"] == pytest.approx(-5.0)
        assert trade["entry_notional"] == pytest.approx(600.0)
        assert trade["net_return"] == pytest.approx(-5.0 / 600.0)

    def test_a_reused_group_id_nets_each_round_trip_separately(self) -> None:
        second = START + pd.Timedelta(hours=3)
        later = {"entry_at": second, "exit_at": second + pd.Timedelta(hours=2)}
        events = _events(
            _round_trip("PERP", -30.0, group_id="funding_arb"),
            _round_trip("SPOT", 25.0, group_id="funding_arb"),
            _round_trip("PERP", 10.0, group_id="funding_arb", **later),
            _round_trip("SPOT", -8.0, group_id="funding_arb", **later),
        )

        trades = closed_trades(events, NO_FINANCING)

        assert trades["net_pnl"].tolist() == pytest.approx([-5.0, 2.0])

    def test_rows_follow_exit_order_across_groups_and_single_trades(self) -> None:
        events = _events(
            _round_trip("A", -10.0, group_id="hedge-1", exit_at=START + pd.Timedelta(hours=4)),
            _round_trip("B", 10.0, group_id="hedge-1", exit_at=START + pd.Timedelta(hours=5)),
            _round_trip("C", 5.0, exit_at=START + pd.Timedelta(hours=3)),
        )

        trades = closed_trades(events, NO_FINANCING)

        assert trades["symbol"].tolist() == ["C", "A / B"]
        assert trades["exit_at"].iloc[1] == START + pd.Timedelta(hours=5)

    def test_no_events_give_no_trades(self) -> None:
        trades = closed_trades(pd.DataFrame(), NO_FINANCING)

        assert trades.empty
        assert "net_return" in trades.columns

    def test_an_exit_without_a_positive_price_is_refused(self) -> None:
        events = _events(_round_trip("A", 0.0))
        events.loc[events["event_type"] == "close", "price"] = 0.0

        with pytest.raises(ValueError, match="price must be positive"):
            closed_trades(events, NO_FINANCING)

    def test_an_exit_without_its_entry_costs_is_refused(self) -> None:
        events = _events(_round_trip("A", 0.0))
        events.loc[events["event_type"] == "close", "entry_commission"] = None

        with pytest.raises(ValueError, match="entry_commission"):
            closed_trades(events, NO_FINANCING)


class TestComputeAllWithEngine:
    """Integration: engine.run() → build_output() uses compute_all internally."""

    def test_engine_result_to_metrics(self) -> None:
        n = 100
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        prices = 100.0 + np.cumsum(np.random.default_rng(42).normal(0.5, 1, n))
        mi = pd.MultiIndex.from_arrays(
            [["TEST"] * n, idx],
            names=["symbol", "datetime"],
        )
        df = pd.DataFrame(
            {
                "open": prices,
                "high": prices * 1.001,
                "low": prices * 0.999,
                "close": prices,
                "volume": np.full(n, 100.0),
            },
            index=mi,
        )

        class BuyBar10CloseBar30(Strategy):
            def on_bar(self, ctx):
                if ctx.period_index == 10 and ctx.symbol not in ctx.positions:
                    return [OrderIntent(action="long", symbol=ctx.symbol)]
                if ctx.period_index == 30 and ctx.symbol in ctx.positions:
                    return [OrderIntent(action="close", symbol=ctx.symbol)]
                return []

        cost = CostModel(
            multiplier=1.0,
            commission_rate=0.001,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
        )
        bt = Backtest(
            df, BuyBar10CloseBar30(), initial_balance=10_000, cost_model=cost, data_source="test"
        )
        bt.run()
        output = bt.build_output()

        m = output.metrics
        assert isinstance(m, StrategyMetrics)
        assert m.trades >= 1
        assert isinstance(m.max_drawdown, float)


def test_compute_payoff_ratio_none_when_one_sided():
    pnls = [
        _trade(net_pnl=10, net_return=0.1),
        _trade(net_pnl=5, net_return=0.05),
    ]
    m = _call_compute_all([10_000.0, 10_010.0, 10_015.0], pnls)
    assert m.payoff_ratio is None


def test_compute_payoff_ratio_value():
    pnls = [
        _trade(net_pnl=20, net_return=0.2),
        _trade(net_pnl=-10, net_return=-0.1),
    ]
    m = _call_compute_all([10_000.0, 10_200.0, 10_100.0], pnls)
    assert np.isclose(m.payoff_ratio, 2.0)


def _signal_fixture_ohlcv() -> pd.DataFrame:
    """Return the shared six-bar signal-outcome contract fixture."""
    return make_signal_outcome_contract_ohlcv()


class TestComputeSignalOutcomes:
    def test_empty_signals(self):
        df = compute_signal_outcomes([], _signal_fixture_ohlcv(), max_periods=3)
        assert df.empty
        assert list(df.columns) == ["ts", "bar_offset", "forward_return", "mfe", "mae"]

    def test_long_signal_walks_next_bar_fill(self):
        """Signal fires at idx[0]; fill is idx[1] (next bar's open), and the
        curve walks idx[2..4] — matches the next-observed-bar reference contract."""
        ohlcv = _signal_fixture_ohlcv()
        df = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=3)

        assert len(df) == 3
        assert list(df["bar_offset"]) == [1, 2, 3]
        for column, expected in SIGNAL_OUTCOME_LONG_PERCENTAGE_POINTS.items():
            assert np.allclose(df[column], expected)
            assert np.allclose(
                df[column] / PERCENTAGE_POINTS_PER_FRACTION,
                SIGNAL_OUTCOME_LONG_FRACTIONS[column],
            )

    def test_short_signal_flips_direction(self):
        ohlcv = _signal_fixture_ohlcv()
        df = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=1, direction="short")

        assert np.allclose(df["forward_return"], [-5.0])
        assert np.allclose(df["mfe"], [5.0])
        assert np.allclose(df["mae"], [10.0])

    def test_signal_near_end_of_data_yields_fewer_rows_not_nan(self):
        ohlcv = _signal_fixture_ohlcv()
        # Signal at idx[3]: fill is idx[4], only idx[5] remains forward -> 1 row.
        df = compute_signal_outcomes([ohlcv.index[3]], ohlcv, max_periods=5)
        assert len(df) == 1
        assert df["bar_offset"].iloc[0] == 1
        assert not df.isna().any().any()

    def test_signal_with_reference_on_last_bar_returns_empty(self):
        ohlcv = _signal_fixture_ohlcv()
        df = compute_signal_outcomes([ohlcv.index[-2]], ohlcv, max_periods=3)
        assert df.empty

    def test_price_col_respected(self):
        ohlcv = _signal_fixture_ohlcv()
        df_open = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=1, price_col="open")
        df_close = compute_signal_outcomes(
            [ohlcv.index[0]], ohlcv, max_periods=1, price_col="close"
        )
        assert not np.allclose(df_open["forward_return"], df_close["forward_return"])
        assert np.allclose(df_close["forward_return"], [(105.0 - 101.0) / 101.0 * 100.0])

    @pytest.mark.parametrize(
        ("direction", "expected_mfe", "expected_mae"),
        [("long", 0.0, 2.0), ("short", 2.0, 0.0)],
    )
    def test_excursions_have_zero_floor(self, direction, expected_mfe, expected_mae):
        index = pd.date_range("2026-03-01", periods=3, freq="1h", tz="UTC")
        ohlcv = pd.DataFrame(
            {
                "open": [100.0, 100.0, 98.0],
                "high": [100.0, 100.0, 99.0],
                "low": [100.0, 100.0, 98.0],
                "close": [100.0, 100.0, 98.0],
            },
            index=index,
        )
        outcome = compute_signal_outcomes(
            [index[0]], ohlcv, max_periods=1, direction=direction
        ).iloc[0]
        assert np.isclose(outcome["mfe"], expected_mfe)
        assert np.isclose(outcome["mae"], expected_mae)

    def test_timezone_aware_signal_is_compared_as_same_instant(self):
        ohlcv = _signal_fixture_ohlcv()
        taipei_signal = ohlcv.index[0].tz_convert(ZoneInfo("Asia/Taipei"))
        utc = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=1)
        taipei = compute_signal_outcomes([taipei_signal], ohlcv, max_periods=1)
        assert np.allclose(
            utc[["forward_return", "mfe", "mae"]],
            taipei[["forward_return", "mfe", "mae"]],
        )

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"direction": "flat"}, "direction"),
            ({"max_periods": 0}, "max_periods"),
            ({"price_col": "vwap"}, "price_col"),
        ],
    )
    def test_invalid_contract_parameters_fail(self, kwargs, message):
        params = {"max_periods": 1, **kwargs}
        with pytest.raises(ValueError, match=message):
            compute_signal_outcomes(
                [_signal_fixture_ohlcv().index[0]],
                _signal_fixture_ohlcv(),
                **params,
            )

    def test_unsorted_or_duplicate_ohlcv_fails(self):
        ohlcv = _signal_fixture_ohlcv()
        with pytest.raises(ValueError, match="sorted"):
            compute_signal_outcomes([ohlcv.index[0]], ohlcv.iloc[::-1], max_periods=1)
        duplicated = pd.concat([ohlcv, ohlcv.iloc[[-1]]])
        with pytest.raises(ValueError, match="unique"):
            compute_signal_outcomes([ohlcv.index[0]], duplicated, max_periods=1)

    def test_timezone_awareness_must_match(self):
        ohlcv = _signal_fixture_ohlcv()
        with pytest.raises(ValueError, match="timezone-aware"):
            compute_signal_outcomes([ohlcv.index[0].tz_localize(None)], ohlcv, max_periods=1)

    def test_nat_signal_fails(self):
        with pytest.raises(ValueError, match="must not be NaT"):
            compute_signal_outcomes([pd.NaT], _signal_fixture_ohlcv(), max_periods=1)


def test_kernel_shared_by_trade_entry_and_signal_outcomes():
    """Equivalent trade and signal anchors share one walk-forward contract."""
    from librae.backtest.schema import PositionEventRecord

    ohlcv = _signal_fixture_ohlcv()
    entry_ts = ohlcv.index[1]  # matches the signal fixture's resolved fill bar
    entry_price = 100.0

    ev_open = PositionEventRecord(
        event_id="e1",
        ts=entry_ts,
        account_id="default",
        currency="USD",
        symbol="X",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=entry_price,
        entry_price=entry_price,
        remaining_quantity=1.0,
        notional=entry_price,
    )
    ev_close = PositionEventRecord(
        event_id="e2",
        ts=ohlcv.index[-1],
        account_id="default",
        currency="USD",
        symbol="X",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=100.0,
        entry_price=entry_price,
        remaining_quantity=0.0,
        notional=100.0,
        realized_pnl=0.0,
    )

    trade_curve = compute_trade_entry_outcomes([ev_open, ev_close], {"X": ohlcv}, max_periods=3)
    signal_df = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=3, price_col="open")

    assert np.allclose(trade_curve["mfe"], signal_df["mfe"])
    assert np.allclose(trade_curve["mae"], signal_df["mae"])
