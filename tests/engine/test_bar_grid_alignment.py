"""Primaries must share their bar starts, since every mode orders events by them.

When one symbol's bar starts while another's is still open, the bar read at
the earlier start closes after the other symbol's next bar opens: a backtest
fills there before the decision could have been made, and live meets the
bars out of start order.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae import Backtest, MarketDataSubscription
from librae.core.cost_model import CostModel
from librae.core.market_data import require_aligned_bar_grids
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader

from tests.conftest import make_test_cfg


def _subscription(symbol: str, timeframe: str, calendar_id: str, session_mode: str):
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source="fixture",
        instrument_type="spot",
    )


@pytest.mark.parametrize(
    "grids",
    [
        pytest.param(
            [("M5", "24/7", "extended"), ("M5", "XTAIFEX", "extended")],
            id="24/7 and TAIFEX five-minute bars share their starts",
        ),
        pytest.param(
            [("M30", "24/7", "extended"), ("M30", "XNYS", "regular")],
            id="XNYS regular hours open on the half hour",
        ),
        pytest.param(
            [("H1", "XNYS", "extended"), ("H1", "XNYS", "extended")],
            id="one calendar and session mode",
        ),
        pytest.param(
            [("D1", "24/7", "regular"), ("D1", "24/7", "extended")],
            id="a full-day calendar reads alike in both modes",
        ),
        pytest.param(
            [("H1", "XNYS", "regular"), ("H1", "XNAS", "regular")],
            id="two calendars with the same sessions, hourly",
        ),
        pytest.param(
            [("D1", "XNYS", "regular"), ("D1", "XNAS", "regular")],
            id="two calendars with the same sessions, daily",
        ),
        pytest.param(
            [("D1", "XNYS", "regular"), ("D1", "XNYS", "extended")],
            id="one calendar's sessions in both modes, daily",
        ),
        pytest.param(
            [("M1", "XTAIFEX", "extended"), ("M1", "XTAI", "extended")],
            id="TAIFEX minutes against an extended TAIEX minute index",
        ),
        pytest.param(
            [("M5", "XTAIFEX", "extended"), ("M5", "XTAI", "extended")],
            id="TAIFEX five minutes against an extended TAIEX index",
        ),
        pytest.param(
            [("M5", "24/7", "extended"), ("M5", "XNYS", "extended")],
            id="extended XNYS opens on the five-minute clock grid",
        ),
    ],
)
def test_aligned_grids_are_accepted(grids: list[tuple[str, str, str]]) -> None:
    require_aligned_bar_grids([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


@pytest.mark.parametrize(
    "grids",
    [
        pytest.param(
            [("D1", "24/7", "regular"), ("D1", "XNYS", "regular")],
            id="a 24/7 day spans the XNYS session",
        ),
        pytest.param(
            [("H1", "24/7", "regular"), ("H1", "XNYS", "regular")],
            id="XNYS hours start on the half hour",
        ),
        pytest.param(
            [("H1", "24/7", "extended"), ("H1", "XTAIFEX", "extended")],
            id="TAIFEX hours start at a quarter to",
        ),
        pytest.param(
            [("H1", "XNYS", "regular"), ("H1", "XNYS", "extended")],
            id="extended hours run on the clock, regular ones on the half hour",
        ),
        pytest.param(
            [("D1", "XTAIFEX", "extended"), ("D1", "XTAI", "extended")],
            id="a TAIFEX day opens the evening before the TAIEX day",
        ),
        pytest.param(
            [("H1", "XTAIFEX", "extended"), ("H1", "XTAI", "extended")],
            id="TAIFEX hours start at a quarter to, TAIEX on the hour",
        ),
    ],
)
def test_misaligned_grids_are_refused(grids: list[tuple[str, str, str]]) -> None:
    with pytest.raises(ValueError, match="one shared grid"):
        require_aligned_bar_grids([_subscription(f"S{i}", *grid) for i, grid in enumerate(grids)])


def _frame(symbol: str, starts: list[str], closes: list[float]) -> pd.DataFrame:
    index = pd.DatetimeIndex(pd.to_datetime(starts, utc=True), name="datetime")
    close = np.array(closes, dtype=float)
    frame = pd.DataFrame(
        {
            "open": 100.0,
            "high": np.maximum(close, 100.0) + 1,
            "low": 99.0,
            "close": close,
            "volume": 100.0,
        },
        index=index,
    )
    frame["symbol"] = symbol
    return frame.set_index("symbol", append=True).reorder_levels(["symbol", "datetime"])


class _Idle(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def test_a_backtest_refuses_a_24_7_day_against_an_xnys_day() -> None:
    """A's Tuesday bar closes at Wednesday 00:00 UTC; a decision on it would
    fill B at Tuesday's 14:30 open."""
    days = ["2026-01-05", "2026-01-06", "2026-01-07"]
    data = pd.concat(
        [
            _frame("A", days, [100, 150, 150]),
            _frame("B", [f"{day} 14:30" for day in days], [100, 100, 100]),
        ]
    )
    with pytest.raises(ValueError, match="one shared grid"):
        Backtest(
            data,
            _Idle(),
            strategy_name="grid",
            cost_model=CostModel.zero(),
            primary_subscriptions=(
                _subscription("A", "D1", "24/7", "regular"),
                _subscription("B", "D1", "XNYS", "regular"),
            ),
        )


def test_live_refuses_misaligned_grids_at_construction() -> None:
    symbols = ["A", "B"]
    calendars = {"A": "24/7", "B": "XNYS"}
    with pytest.raises(ValueError, match="one shared grid"):
        LiveTrader(
            _Idle(),
            lambda frame: frame,
            config=make_test_cfg(
                mode="sim",
                symbols=symbols,
                timeframe="H1",
                session_mode="regular",
                instrument_overrides={
                    symbol: {
                        "instrument_type": "spot",
                        "currency": "USDT",
                        "calendar_id": calendars[symbol],
                    }
                    for symbol in symbols
                },
                symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in symbols},
            ),
            adapter={symbol: (lambda *_a, **_k: pd.DataFrame()) for symbol in symbols},
            cost_model=CostModel.zero(),
        )
