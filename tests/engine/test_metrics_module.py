"""Tests for the metrics module.

Tests compute_all() which accepts primitive sequences (equity values,
timestamps, TradePnL objects).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.backtest.schema import StrategyMetrics
from librae.core.cost_model import CostModel
from librae.core.metrics import (
    PERCENTAGE_POINTS_PER_FRACTION,
    compute_all,
    compute_signal_outcomes,
    compute_trade_entry_outcomes,
    generate_signal_mae_mfe_report,
    summarize_signal_mae_mfe,
)
from librae.core.strategy import Action, BaseStrategy
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
    assert librae.summarize_signal_mae_mfe is summarize_signal_mae_mfe
    assert librae.generate_signal_mae_mfe_report is generate_signal_mae_mfe_report


def _make_trade_pnl(
    gross_pnl: float = 0.0,
    net_pnl: float = 0.0,
    commission: float = 0.0,
    slippage: float = 0.0,
    tax: float = 0.0,
    net_return: float = 0.0,
) -> SimpleNamespace:
    """Duck-typed TradePnL for tests."""
    return SimpleNamespace(
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        commission=commission,
        slippage=slippage,
        tax=tax,
        gross_return=0.0,
        net_return=net_return,
        exit_commission=0.0,
        exit_slippage=0.0,
        exit_tax=tax,
    )


def _call_compute_all(
    equity: list[float],
    trade_pnls: list | None = None,
    exposed_periods: int | None = None,
    annualize: bool = False,
) -> StrategyMetrics:
    """Helper to call compute_all with timestamps derived from equity length."""
    timestamps = pd.date_range(START, periods=len(equity), freq="h", tz="UTC").tolist()
    return compute_all(
        equity_values=equity,
        timestamps=timestamps,
        trade_pnls=trade_pnls or [],
        total_periods=len(equity),
        annualize=annualize,
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

    def test_no_trades_keeps_time_series_and_benchmark_metrics(self) -> None:
        equity = np.array([10_000.0, 10_500.0, 9_500.0, 10_000.0])
        timestamps = pd.date_range(START, periods=len(equity), freq="h", tz="UTC").tolist()

        m = compute_all(
            equity_values=equity,
            timestamps=timestamps,
            trade_pnls=[],
            total_periods=len(equity),
            benchmark_values=np.array([10_000.0, 10_100.0, 10_200.0, 10_300.0]),
        )

        assert m.trades == 0
        assert m.max_drawdown < 0
        assert m.benchmark_return == pytest.approx(0.03)


class TestComputeAllMetrics:
    def test_positive_return(self) -> None:
        pnl = _make_trade_pnl(gross_pnl=100, net_pnl=100, net_return=1.0)
        m = _call_compute_all([10_000.0, 10_000.0, 10_100.0], [pnl])
        assert m.trades == 1
        assert m.total_return > 0

    def test_win_rate(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=10, net_return=0.1),  # win
            _make_trade_pnl(net_pnl=-10, net_return=-0.1),  # loss
            _make_trade_pnl(net_pnl=5, net_return=0.05),  # win
        ]
        m = _call_compute_all([10_000.0, 10_100.0, 9_900.0, 10_050.0], pnls)
        assert np.isclose(m.win_rate, 2 / 3)

    def test_profit_factor(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=20, net_return=0.2),
            _make_trade_pnl(net_pnl=-10, net_return=-0.1),
        ]
        m = _call_compute_all([10_000.0, 10_200.0, 10_100.0], pnls)
        assert np.isclose(m.profit_factor, 2.0, atol=1e-6)

    def test_exposure_ratio(self) -> None:
        pnl = _make_trade_pnl(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0] * 20, [pnl], exposed_periods=5)
        assert np.isclose(m.exposure_ratio, 5 / 20)

    def test_cost_totals(self) -> None:
        pnl = _make_trade_pnl(
            gross_pnl=10.0,
            net_pnl=6.5,
            commission=2.0,
            slippage=1.0,
            tax=0.5,
            net_return=0.065,
        )
        m = _call_compute_all([10_000.0, 10_003.0, 10_006.5], [pnl])
        assert np.isclose(m.total_commission, 2.0)
        assert np.isclose(m.total_slippage, 1.0)

    def test_portfolio_diagnostics(self) -> None:
        timestamps = pd.date_range(START, periods=3, freq="h", tz="UTC").tolist()
        m = compute_all(
            equity_values=[100.0, 101.0, 102.0],
            timestamps=timestamps,
            trade_pnls=[],
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

    def test_tracking_error_and_information_ratio_use_active_returns(self) -> None:
        timestamps = pd.date_range(START, periods=4, freq="D", tz="UTC").tolist()
        equity = [100.0, 102.0, 101.0, 104.0]
        benchmark = [100.0, 101.0, 102.0, 103.0]

        m = compute_all(
            equity_values=equity,
            timestamps=timestamps,
            trade_pnls=[],
            total_periods=4,
            benchmark_values=benchmark,
        )

        strategy_returns = np.diff(equity) / np.asarray(equity[:-1])
        benchmark_returns = np.diff(benchmark) / np.asarray(benchmark[:-1])
        active_returns = strategy_returns - benchmark_returns
        active_std = np.std(active_returns, ddof=1)
        assert m.tracking_error == pytest.approx(active_std * np.sqrt(365))
        assert m.information_ratio == pytest.approx(
            np.mean(active_returns) / active_std * np.sqrt(365)
        )

    def test_sharpe_is_float(self) -> None:
        pnls = [
            _make_trade_pnl(net_pnl=100, net_return=1.0),
            _make_trade_pnl(net_pnl=80, net_return=0.8),
            _make_trade_pnl(net_pnl=50, net_return=0.5),
        ]
        m = _call_compute_all([10_000.0, 10_100.0, 10_180.0, 10_230.0], pnls, annualize=True)
        assert isinstance(m.sharpe, float)

    def test_max_drawdown_negative(self) -> None:
        pnl = _make_trade_pnl(net_pnl=10, net_return=0.1)
        m = _call_compute_all([10_000.0, 10_500.0, 9_800.0, 10_200.0], [pnl])
        assert m.max_drawdown <= 0

    def test_annual_metrics_share_temporal_parameters(self, monkeypatch) -> None:
        import quantstats as qs

        calls: dict[str, dict[str, float | int]] = {}

        def recorder(name: str):
            def metric(_returns, **kwargs):
                calls[name] = kwargs
                return 1.0

            return metric

        for name in ("sharpe", "sortino", "calmar", "cagr"):
            monkeypatch.setattr(qs.stats, name, recorder(name))

        timestamps = pd.date_range(START, periods=4, freq="D", tz="UTC").tolist()
        compute_all(
            equity_values=[10_000.0, 10_100.0, 10_050.0, 10_200.0],
            timestamps=timestamps,
            trade_pnls=[_make_trade_pnl(net_pnl=200.0)],
            total_periods=4,
            annualize=True,
            risk_free_rate=0.03,
        )

        periods = calls["sharpe"]["periods"]
        assert calls["sharpe"] == {"periods": periods, "rf": 0.03}
        assert calls["sortino"] == {"periods": periods, "rf": 0.03}
        assert calls["calmar"] == {"periods": periods}
        assert calls["cagr"] == {"periods": periods}


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

        class BuyBar10CloseBar30(BaseStrategy):
            def on_bar(self, ctx):
                if ctx.period_index == 10 and ctx.symbol not in ctx.positions:
                    return [Action(type="long", symbol=ctx.symbol)]
                if ctx.period_index == 30 and ctx.symbol in ctx.positions:
                    return [Action(type="close", symbol=ctx.symbol)]
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
        _make_trade_pnl(net_pnl=10, net_return=0.1),
        _make_trade_pnl(net_pnl=5, net_return=0.05),
    ]
    m = _call_compute_all([10_000.0, 10_010.0, 10_015.0], pnls)
    assert m.payoff_ratio is None


def test_compute_payoff_ratio_value():
    pnls = [
        _make_trade_pnl(net_pnl=20, net_return=0.2),
        _make_trade_pnl(net_pnl=-10, net_return=-0.1),
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


class TestSummarizeSignalMaeMfe:
    def test_empty_horizons(self):
        df = summarize_signal_mae_mfe(
            [datetime(2026, 3, 1, tzinfo=UTC)], _signal_fixture_ohlcv(), horizons=()
        )
        assert df.empty

    def test_single_signal_matches_hand_computed_curve(self):
        ohlcv = _signal_fixture_ohlcv()
        summary = summarize_signal_mae_mfe(
            [ohlcv.index[0]], ohlcv, horizons=(1, 2, 3), direction="long"
        )

        assert list(summary["horizon"]) == [1, 2, 3]
        assert (summary["n"] == 1).all()
        # n=1 -> median == p75 == the single observation.
        assert np.allclose(summary["median_forward_return"], [5.0, 2.0, 0.0])
        assert np.allclose(summary["median_mfe"], [10.0, 10.0, 10.0])
        assert np.allclose(summary["p75_mfe"], summary["median_mfe"])
        assert np.allclose(summary["median_mae"], [5.0, 5.0, 5.0])
        assert np.allclose(summary["p75_mae"], summary["median_mae"])

    def test_horizon_beyond_available_data_is_dropped(self):
        ohlcv = _signal_fixture_ohlcv()
        # Only 3 forward bars exist after the fill bar; horizon 10 has no data.
        summary = summarize_signal_mae_mfe([ohlcv.index[0]], ohlcv, horizons=(1, 10))
        assert list(summary["horizon"]) == [1]

    def test_sample_count_is_reported_per_horizon(self):
        ohlcv = _signal_fixture_ohlcv()
        summary = summarize_signal_mae_mfe([ohlcv.index[0], ohlcv.index[3]], ohlcv, horizons=(1, 3))
        assert list(summary["n"]) == [2, 1]

    @pytest.mark.parametrize("horizons", [(0,), (1, 1)])
    def test_invalid_horizons_fail(self, horizons):
        with pytest.raises(ValueError, match="horizon"):
            summarize_signal_mae_mfe(
                [_signal_fixture_ohlcv().index[0]],
                _signal_fixture_ohlcv(),
                horizons=horizons,
            )


def test_generate_signal_report_states_assumptions_and_sample_counts(tmp_path):
    ohlcv = _signal_fixture_ohlcv()
    output_path = tmp_path / "signal-outcomes.html"

    result = generate_signal_mae_mfe_report(
        [ohlcv.index[0], ohlcv.index[3]],
        ohlcv,
        output_path=str(output_path),
        horizons=(1, 3),
        direction="long",
        price_col="open",
    )

    html = output_path.read_text(encoding="utf-8")
    assert result == str(output_path)
    assert "Gross hypothetical outcomes" in html
    assert "reference=next observed open" in html
    assert "T+1: 2, T+3: 1" in html


def test_kernel_shared_by_trade_entry_and_signal_outcomes():
    """Equivalent trade and signal anchors share one walk-forward contract."""
    from librae.backtest.schema import OrderEventRecord

    ohlcv = _signal_fixture_ohlcv()
    entry_ts = ohlcv.index[1]  # matches the signal fixture's resolved fill bar
    entry_price = 100.0

    ev_open = OrderEventRecord(
        event_id="e1",
        ts=entry_ts,
        symbol="X",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=entry_price,
        entry_price=entry_price,
        remaining_quantity=1.0,
        notional=entry_price,
    )
    ev_close = OrderEventRecord(
        event_id="e2",
        ts=ohlcv.index[-1],
        symbol="X",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=100.0,
        entry_price=entry_price,
        remaining_quantity=0.0,
        notional=100.0,
        pnl=0.0,
    )

    trade_curve = compute_trade_entry_outcomes([ev_open, ev_close], {"X": ohlcv}, max_periods=3)
    signal_df = compute_signal_outcomes([ohlcv.index[0]], ohlcv, max_periods=3, price_col="open")

    assert np.allclose(trade_curve["mfe"], signal_df["mfe"])
    assert np.allclose(trade_curve["mae"], signal_df["mae"])
