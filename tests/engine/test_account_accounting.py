from __future__ import annotations

from dataclasses import replace
from datetime import UTC

import pandas as pd
import pytest
from librae import (
    AccountConfig,
    Backtest,
    Context,
    CostModel,
    ExecutionPolicy,
    MultiLegOrder,
    OrderIntent,
    PortfolioTargets,
    RiskPolicy,
    RunConfig,
    Strategy,
)
from librae.core.executor import REASON_DRAWDOWN_BREACH


def _frame() -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01", periods=5, freq="h", tz=UTC)
    rows = []
    for symbol, prices in {
        "AAA": [100.0, 100.0, 105.0, 108.0, 110.0],
        "BBB": [200.0, 200.0, 195.0, 192.0, 190.0],
    }.items():
        for ts, price in zip(timestamps, prices, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "datetime": ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1_000.0,
                }
            )
    return pd.DataFrame(rows).set_index(["symbol", "datetime"]).sort_index()


def _config() -> RunConfig:
    return RunConfig(
        strategy_name="two_accounts",
        symbols=("AAA", "BBB"),
        timeframe="H1",
        market="multi",
        data_source="multi",
        accounts={
            "alpha": AccountConfig(currency="USD", initial_cash=1_000.0),
            "beta": AccountConfig(currency="USD", initial_cash=1_000.0),
        },
        mode="backtest",
        execution=ExecutionPolicy(max_bar_volume_participation_rate=None),
        symbol_cost_overrides={
            "AAA": {"multiplier": 1.0},
            "BBB": {"multiplier": 1.0},
        },
        instrument_overrides={
            "AAA": {
                "account_id": "alpha",
                "currency": "USD",
                "instrument_type": "spot",
                "market": "test",
                "data_source": "binance_spot",
                "data_adapter": "crypto",
            },
            "BBB": {
                "account_id": "beta",
                "currency": "USD",
                "instrument_type": "spot",
                "market": "test",
                "data_source": "binance_spot",
                "data_adapter": "crypto",
            },
        },
        annualize=False,
        no_db=True,
    )


class _TwoLegStrategy(Strategy):
    def on_bar(self, ctx: Context):
        if ctx.period_index != 0:
            return []
        return MultiLegOrder(
            legs=(
                OrderIntent(action="long", symbol="AAA", quantity=1.0),
                OrderIntent(action="long", symbol="BBB", quantity=1.0),
            )
        )


def test_same_currency_accounts_keep_separate_pnl_and_metrics() -> None:
    backtest = Backtest(
        _frame(),
        _TwoLegStrategy(),
        config=_config(),
        cost_model=CostModel.zero(),
    )

    result = backtest.run()
    output = backtest.build_output()

    raw_accounts = {account.account_id: account for account in result.accounts}
    assert raw_accounts["alpha"].final_equity == pytest.approx(1_010.0)
    assert raw_accounts["beta"].final_equity == pytest.approx(990.0)

    accounts = {account.account_id: account for account in output.accounts}
    assert accounts["alpha"].currency == "USD"
    assert accounts["alpha"].net_pnl == pytest.approx(10.0)
    assert accounts["beta"].currency == "USD"
    assert accounts["beta"].net_pnl == pytest.approx(-10.0)
    assert {(event.account_id, event.currency) for event in output.order_events} == {
        ("alpha", "USD"),
        ("beta", "USD"),
    }
    with pytest.raises(ValueError, match="undefined for multiple accounts"):
        _ = output.metrics
    with pytest.raises(ValueError, match="undefined for multiple accounts"):
        _ = backtest.metrics


def test_multi_account_benchmark_requires_account_specific_evaluation() -> None:
    backtest = Backtest(
        _frame(),
        _TwoLegStrategy(),
        config=_config(),
        cost_model=CostModel.zero(),
    )

    with pytest.raises(ValueError, match="ambiguous for multiple accounts"):
        backtest.add_benchmark(pd.Series([100.0, 101.0]))


class _CrossAccountWeights(Strategy):
    def on_bar(self, ctx: Context):
        if ctx.period_index == 0:
            return PortfolioTargets(weights={"AAA": 0.5, "BBB": 0.5})
        return []


def test_portfolio_targets_reject_cross_account_capital_base() -> None:
    backtest = Backtest(
        _frame(),
        _CrossAccountWeights(),
        config=_config(),
        cost_model=CostModel.zero(),
    )

    with pytest.raises(ValueError, match="cannot span isolated accounts"):
        backtest.run()


def test_drawdown_halt_is_scoped_to_breached_account() -> None:
    timestamps = pd.date_range("2026-01-01", periods=5, freq="h", tz=UTC)
    rows = []
    for symbol, prices in {
        "AAA": [100.0, 100.0, 50.0, 50.0, 50.0],
        "BBB": [100.0, 100.0, 100.0, 100.0, 100.0],
    }.items():
        for ts, price in zip(timestamps, prices, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "datetime": ts,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": 1_000.0,
                }
            )
    data = pd.DataFrame(rows).set_index(["symbol", "datetime"]).sort_index()

    class OpenBothAccounts(Strategy):
        def __init__(self) -> None:
            self.calls = 0

        def on_bar(self, ctx: Context):
            self.calls += 1
            if ctx.period_index == 0:
                return MultiLegOrder(
                    legs=(
                        OrderIntent(action="long", symbol="AAA", quantity=10.0),
                        OrderIntent(action="long", symbol="BBB", quantity=10.0),
                    )
                )
            return [
                OrderIntent(action="long", symbol="AAA", quantity=1.0),
            ]

    strategy = OpenBothAccounts()
    config = replace(
        _config(),
        risk=RiskPolicy(max_drawdown_rate=0.2),
    )
    backtest = Backtest(
        data,
        strategy,
        config=config,
        cost_model=CostModel.zero(),
    )

    result = backtest.run()
    output = backtest.build_output()

    accounts = {account.account_id: account for account in result.accounts}
    assert accounts["alpha"].final_equity == pytest.approx(500.0)
    assert accounts["beta"].final_equity == pytest.approx(1_000.0)
    assert strategy.calls > 2
    drawdown_exits = [
        event
        for event in output.order_events
        if event.reason == REASON_DRAWDOWN_BREACH and event.event_type == "close"
    ]
    assert {event.symbol for event in drawdown_exits} == {"AAA"}
