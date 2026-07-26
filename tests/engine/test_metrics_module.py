"""Tests for the metrics module.

Tests compute_all() which accepts primitive sequences (equity values,
timestamps, TradePnL objects).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
from librae.backtest.engine import Backtest
from librae.backtest.schema import StrategyMetrics
from librae.core.cost_model import CostModel
from librae.core.metrics import compute_all
from librae.core.strategy import Action, BaseStrategy

START = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
END = datetime(2024, 2, 1, 0, 0, 0, tzinfo=UTC)


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


def test_compute_trade_mae_mfe():
    from librae.backtest.schema import OrderEventRecord
    from librae.core.metrics import compute_trade_mae_mfe

    idx = pd.date_range("2026-03-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100] * 10,
            "high": [100, 105, 110, 102, 100, 100, 108, 100, 100, 100],
            "low": [100, 95, 90, 98, 100, 100, 92, 100, 100, 100],
            "close": [100] * 10,
        },
        index=idx,
    )

    ev_open = OrderEventRecord(
        event_id="e1",
        ts=idx[0],
        symbol="BTCUSDT",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=100.0,
        entry_price=100.0,
        remaining_quantity=1.0,
        notional=100.0,
    )
    ev_close = OrderEventRecord(
        event_id="e2",
        ts=idx[3],
        symbol="BTCUSDT",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=102.0,
        entry_price=100.0,
        remaining_quantity=0.0,
        notional=102.0,
    )

    res = compute_trade_mae_mfe([ev_open, ev_close], df)
    assert res["n"] == 1
    assert res["median_mfe"] == 10.0  # max high is 110 at idx[2], (110-100)/100 = +10%
    assert res["median_mae"] == 10.0  # min low is 90 at idx[2], (100-90)/100 = 10%


def test_compute_trade_mae_mfe_short():
    from librae.backtest.schema import OrderEventRecord
    from librae.core.metrics import compute_trade_mae_mfe

    idx = pd.date_range("2026-03-01", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100] * 5,
            "high": [100, 103, 120, 102, 100],  # spikes up to 120 -> adverse for a short
            "low": [100, 99, 98, 95, 100],  # dips to 95 -> favorable for a short
            "close": [100] * 5,
        },
        index=idx,
    )

    ev_open = OrderEventRecord(
        event_id="e1",
        ts=idx[0],
        symbol="BTCUSDT",
        side="short",
        event_type="open",
        fill_quantity=1.0,
        price=100.0,
        entry_price=100.0,
        remaining_quantity=1.0,
        notional=100.0,
    )
    ev_close = OrderEventRecord(
        event_id="e2",
        ts=idx[3],
        symbol="BTCUSDT",
        side="short",
        event_type="close",
        fill_quantity=1.0,
        price=98.0,
        entry_price=100.0,
        remaining_quantity=0.0,
        notional=98.0,
    )

    res = compute_trade_mae_mfe([ev_open, ev_close], df)
    assert res["n"] == 1
    assert res["median_mae"] == 20.0  # high spiked to 120, adverse move for a short: (120-100)/100
    assert res["median_mfe"] == 5.0  # low dipped to 95, favorable move for a short: (100-95)/100


def test_compute_trade_mae_mfe_envelope_curve():
    """max_periods=N returns a per-offset decay curve, ignoring the trade's actual exit."""
    from librae.backtest.schema import OrderEventRecord
    from librae.core.metrics import compute_trade_mae_mfe

    idx = pd.date_range("2026-03-01", periods=10, freq="1h", tz="UTC")
    df = pd.DataFrame(
        {
            "open": [100] * 10,
            "high": [100, 105, 110, 102, 100, 100, 108, 100, 100, 100],
            "low": [100, 95, 90, 98, 100, 100, 92, 100, 100, 100],
            "close": [100] * 10,
        },
        index=idx,
    )
    ev_open = OrderEventRecord(
        event_id="e1",
        ts=idx[0],
        symbol="BTCUSDT",
        side="long",
        event_type="open",
        fill_quantity=1.0,
        price=100.0,
        entry_price=100.0,
        remaining_quantity=1.0,
        notional=100.0,
    )
    # Closes at idx[3] but the curve should keep walking OHLCV past that point.
    ev_close = OrderEventRecord(
        event_id="e2",
        ts=idx[3],
        symbol="BTCUSDT",
        side="long",
        event_type="close",
        fill_quantity=1.0,
        price=102.0,
        entry_price=100.0,
        remaining_quantity=0.0,
        notional=102.0,
    )

    res = compute_trade_mae_mfe([ev_open, ev_close], df, max_periods=5)
    assert res["n"] == 1
    assert res["offsets"] == [1, 2, 3, 4, 5]
    # T=1 (bar idx[1], high=105): running max so far since entry is still 100 (entry bar).
    assert res["median_mfe_curve"][0] == 5.0
    # T=3 (bar idx[3]): running max high across idx[0..3] is 110 -> +10%.
    assert res["median_mfe_curve"][2] == 10.0
    # T=5 (bar idx[5], past the trade's actual close at idx[3]): still walks forward.
    assert res["median_mfe_curve"][4] == 10.0


def test_compute_trade_mae_mfe_empty():
    from librae.core.metrics import compute_trade_mae_mfe

    assert compute_trade_mae_mfe([], pd.DataFrame())["n"] == 0
    curve = compute_trade_mae_mfe([], pd.DataFrame(), max_periods=10)
    assert curve["n"] == 0
    assert curve["offsets"] == []


def test_compute_trade_durations():
    from librae.backtest.schema import OrderEventRecord
    from librae.core.metrics import compute_trade_durations

    events = [
        OrderEventRecord(
            event_id="o1",
            ts=START,
            symbol="X",
            side="long",
            event_type="open",
            fill_quantity=1.0,
            price=100.0,
            entry_price=100.0,
            remaining_quantity=1.0,
            notional=100.0,
        ),
        OrderEventRecord(
            event_id="c1",
            ts=START,
            symbol="X",
            side="long",
            event_type="close",
            fill_quantity=1.0,
            price=105.0,
            entry_price=100.0,
            remaining_quantity=0.0,
            notional=105.0,
            pnl=5.0,
            periods_held=7,
        ),
        OrderEventRecord(
            event_id="c2",
            ts=START,
            symbol="X",
            side="long",
            event_type="close",
            fill_quantity=1.0,
            price=95.0,
            entry_price=100.0,
            remaining_quantity=0.0,
            notional=95.0,
            pnl=-5.0,
            periods_held=3,
        ),
    ]
    assert compute_trade_durations(events) == [7, 3]


def test_compute_pnl_by_trade():
    from librae.backtest.schema import OrderEventRecord
    from librae.core.metrics import compute_pnl_by_trade

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    t1 = datetime(2026, 1, 2, tzinfo=UTC)
    events = [
        OrderEventRecord(
            event_id="c2",
            ts=t1,
            symbol="X",
            side="long",
            event_type="close",
            fill_quantity=1.0,
            price=95.0,
            entry_price=100.0,
            remaining_quantity=0.0,
            notional=95.0,
            pnl=-5.0,
        ),
        OrderEventRecord(
            event_id="c1",
            ts=t0,
            symbol="X",
            side="long",
            event_type="close",
            fill_quantity=1.0,
            price=105.0,
            entry_price=100.0,
            remaining_quantity=0.0,
            notional=105.0,
            pnl=5.0,
        ),
    ]
    # Out-of-order input, but result must follow ts order (5, then 5-5=0).
    assert compute_pnl_by_trade(events) == [5.0, 0.0]


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
