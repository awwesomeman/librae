"""Crypto spot sells owned inventory, so no engine may open or add a short on it.

The live crypto adapter refuses such an order when it prepares it. Decision
preflight refuses it on the emitting bar in every mode, so a backtest cannot
book PnL from a short that live will never place. The rule is crypto spot
only: equity spot can be sold short on margin, and contracts short normally.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.config.symbols import SymbolInfo, get_symbol
from librae.core.cost_model import CostModel
from librae.core.executor import validate_strategy_decision
from librae.core.strategy import OrderIntent, PortfolioWeights, PositionState, Strategy

from tests.conftest import make_test_cfg

REFUSED = r"shorts \['BTCUSDT'\], which cannot open or add to a short"


def _bars(symbol: str, n: int = 8) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    price = np.full(n, 100.0)
    return pd.DataFrame(
        {"open": price, "high": price, "low": price, "close": price, "volume": np.full(n, 1e6)},
        index=pd.MultiIndex.from_arrays([[symbol] * n, index], names=["symbol", "datetime"]),
    )


class _EnterThenClose(Strategy):
    def __init__(self, action: str) -> None:
        self._action = action

    def on_bar(self, ctx):
        if ctx.period_index == 1:
            return [OrderIntent(action=self._action, symbol=ctx.symbol, quantity=1.0)]
        if ctx.period_index == 4 and ctx.symbol in ctx.positions:
            return [OrderIntent(action="close", symbol=ctx.symbol)]
        return []


class _NegativeWeight(Strategy):
    def on_bar(self, ctx):
        return PortfolioWeights({ctx.symbol: -0.5}) if ctx.period_index == 1 else []


def _run(symbol: str, strategy: Strategy, **kwargs):
    kwargs.setdefault("cost_model", CostModel.zero())
    kwargs.setdefault("data_source", "test")
    return Backtest(_bars(symbol), strategy, **kwargs).run()


def _events(result) -> list[tuple[str, str]]:
    return [(event.event_type, event.side) for event in result.position_events]


class TestCryptoSpotRefusesShorts:
    def test_backtest_refuses_a_short_open(self) -> None:
        with pytest.raises(ValueError, match=REFUSED):
            _run("BTCUSDT", _EnterThenClose("short"), currency="USDT")

    def test_a_configured_run_refuses_it_with_or_without_a_broker(self) -> None:
        for broker in (None, "binance"):
            config = make_test_cfg(mode="backtest", broker=broker)
            with pytest.raises(ValueError, match=REFUSED):
                Backtest(_bars("BTCUSDT"), _EnterThenClose("short"), config=config).run()

    def test_backtest_refuses_a_negative_target_weight(self) -> None:
        with pytest.raises(ValueError, match=REFUSED):
            _run("BTCUSDT", _NegativeWeight(), currency="USDT")

    def test_preflight_refuses_adding_to_a_short(self) -> None:
        held_short = PositionState(
            symbol="BTCUSDT",
            side="short",
            entry_price=100.0,
            quantity=1.0,
            entry_at=pd.Timestamp("2025-01-01", tz="UTC").to_pydatetime(),
            periods_held=1,
            entry_commission=0.0,
            entry_slippage=0.0,
            entry_tax=0.0,
            total_entry_cost=100.0,
        )
        spot = get_symbol("BTCUSDT")

        with pytest.raises(ValueError, match=REFUSED):
            validate_strategy_decision(
                [OrderIntent(action="short", symbol="BTCUSDT", quantity=1.0)],
                {"BTCUSDT"},
                primary_symbol="BTCUSDT",
                bars={"BTCUSDT": {"close": 100.0}},
                positions={"BTCUSDT": held_short},
                can_short=lambda _symbol: spot.can_short,
            )

    @pytest.mark.parametrize(
        ("decision", "remedy", "wrong_remedy"),
        [
            (
                [OrderIntent(action="short", symbol="BTCUSDT", quantity=1.0)],
                "reduce a long with action='close'",
                "target weight",
            ),
            (
                PortfolioWeights({"BTCUSDT": -0.5}),
                "set a non-negative target weight",
                "action='close'",
            ),
        ],
    )
    def test_the_remedy_fits_the_decision_type(self, decision, remedy, wrong_remedy) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_strategy_decision(
                decision,
                {"BTCUSDT"},
                primary_symbol="BTCUSDT",
                bars={"BTCUSDT": {"close": 100.0}},
                positions={},
                can_short=lambda _symbol: False,
            )

        message = str(excinfo.value)
        assert "(crypto spot sells owned inventory)" in message
        assert remedy in message
        assert wrong_remedy not in message
        assert "instrument_overrides" in message

    def test_a_config_market_declares_unregistered_spot_symbols(self) -> None:
        config = make_test_cfg(
            mode="backtest",
            symbols=["COIN"],
            instrument_overrides={
                "COIN": {
                    "data_adapter": "crypto",
                    "instrument_type": "spot",
                    "currency": "USDT",
                    "calendar_id": "24/7",
                }
            },
            symbol_cost_overrides={"COIN": {"multiplier": 1.0}},
        )

        with pytest.raises(ValueError, match=r"shorts \['COIN'\]"):
            Backtest(_bars("COIN"), _EnterThenClose("short"), config=config).run()

    def test_a_long_round_trip_still_runs(self) -> None:
        result = _run("BTCUSDT", _EnterThenClose("long"), currency="USDT")

        assert _events(result) == [("open", "long"), ("close", "long")]


class TestOtherInstrumentsStillShort:
    def test_us_equity_spot_can_be_shorted(self) -> None:
        result = _run("MU", _EnterThenClose("short"))

        assert _events(result) == [("open", "short"), ("close", "short")]

    def test_a_crypto_contract_can_be_shorted(self) -> None:
        result = _run("BTCUSDT_QUARTERLY", _EnterThenClose("short"), currency="USDT")

        assert _events(result) == [("open", "short"), ("close", "short")]

    def test_only_crypto_spot_is_restricted(self) -> None:
        def instrument(market: str, instrument_type: str) -> SymbolInfo:
            return SymbolInfo(
                symbol="X",
                market=market,
                data_source="test",
                instrument_type=instrument_type,
                multiplier=1.0,
                data_adapter="test",
                venue_symbol="X",
                currency="USD",
            )

        assert not instrument("crypto", "spot").can_short
        assert instrument("crypto", "contract_perpetual").can_short
        assert instrument("us_equity", "spot").can_short
        assert instrument("tw_equity", "spot").can_short

    def test_a_direct_symbol_without_metadata_is_not_guessed(self) -> None:
        result = _run("UNREGISTERED", _EnterThenClose("short"))

        assert _events(result) == [("open", "short"), ("close", "short")]
