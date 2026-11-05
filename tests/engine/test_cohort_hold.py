"""A multi-symbol bar is decided once, with every required symbol that is due it.

An adapter releases a bar once the venue has started the next one, and a
venue does that per symbol, seconds apart. A poll landing in between would
decide the bar for some symbols against the others a bar stale, which a
backtest never does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from librae import FeatureBatch
from librae.core.cost_model import CostModel
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.live.engine import LiveTrader

from tests.conftest import make_test_cfg

BAR = pd.Timestamp("2026-01-01T02:00Z")
CLOSE = (BAR + pd.Timedelta(hours=1)).to_pydatetime()


class _Hold(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _bars(through: pd.Timestamp, start: str = "2026-01-01T00:00Z") -> pd.DataFrame:
    ts = pd.date_range(start, through, freq="h")
    return pd.DataFrame(
        {"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}
    )


def _trader(
    newest: dict[str, pd.Timestamp],
    clock: dict[str, datetime],
    decided: list,
    *,
    calendars: dict[str, str] | None = None,
    start: str = "2026-01-01T00:00Z",
):
    symbols = ["A", "B"]
    calendars = calendars or {symbol: "24/7" for symbol in symbols}

    def features(batch: FeatureBatch):
        decided.append(
            (
                pd.Timestamp(batch.event_ts),
                sorted(s.symbol for s in batch.active_primary_subscriptions),
            )
        )
        return {
            subscription: batch.market_data.history(subscription)
            for subscription in batch.active_primary_subscriptions
        }

    return LiveTrader(
        _Hold(),
        None,
        config=make_test_cfg(
            mode="sim",
            symbols=symbols,
            instrument_overrides={
                symbol: {
                    "instrument_type": "spot",
                    "currency": "USDT",
                    "calendar_id": calendars[symbol],
                }
                for symbol in symbols
            },
            symbol_cost_overrides={symbol: {"multiplier": 1.0} for symbol in symbols},
            execution=ExecutionPolicy(max_bar_volume_participation_rate=None, warmup_periods=2),
        ),
        batch_feature_fn=features,
        adapter={
            symbol: (lambda *_args, symbol=symbol, **_kwargs: _bars(newest[symbol], start))
            for symbol in symbols
        },
        cost_model=CostModel.zero(),
        clock=lambda: clock["now"],
    )


def _poll_while_b_is_late(b_has_it_at: timedelta | None, second_poll: timedelta) -> list:
    """A has the 02:00 bar 5 s after its close, B at ``b_has_it_at``."""
    newest = {"A": BAR - pd.Timedelta(hours=1), "B": BAR - pd.Timedelta(hours=1)}
    clock = {"now": CLOSE - timedelta(minutes=30)}
    decided: list = []
    trader = _trader(newest, clock, decided)
    trader._poll_cycle()

    newest["A"] = BAR
    clock["now"] = CLOSE + timedelta(seconds=5)
    trader._poll_cycle()
    clock["now"] = CLOSE + second_poll
    if b_has_it_at is not None and b_has_it_at <= second_poll:
        newest["B"] = BAR
    trader._poll_cycle()
    return decided


def test_a_bar_waits_for_a_required_symbol_due_it():
    decided = _poll_while_b_is_late(
        b_has_it_at=timedelta(seconds=12), second_poll=timedelta(seconds=20)
    )

    assert decided[1:] == [(BAR, ["A", "B"])]


def test_past_the_hold_the_symbols_that_have_it_go_ahead():
    decided = _poll_while_b_is_late(b_has_it_at=None, second_poll=LiveTrader.COHORT_HOLD)

    assert decided[1:] == [(BAR, ["A"])]


def test_a_symbol_whose_market_is_shut_does_not_hold_the_rest():
    """B trades on XNYS, closed from Friday 21:00 UTC to Monday's open."""
    friday = pd.Timestamp("2026-01-02T20:00Z")
    newest = {"A": friday, "B": friday}
    clock = {"now": (friday + pd.Timedelta(minutes=90)).to_pydatetime()}
    decided: list = []
    trader = _trader(
        newest,
        clock,
        decided,
        calendars={"A": "24/7", "B": "XNYS"},
        start="2026-01-02T18:00Z",
    )
    trader._poll_cycle()

    newest["A"] = friday + pd.Timedelta(hours=1)
    clock["now"] = (friday + pd.Timedelta(hours=2, seconds=5)).to_pydatetime()
    trader._poll_cycle()

    assert decided[-1] == (friday + pd.Timedelta(hours=1), ["A"])
