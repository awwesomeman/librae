"""A multi-symbol bar is decided once, with every required symbol that is due it.

An adapter releases a bar once the venue has started the next one, and a
venue does that per symbol, seconds apart. A poll landing in between would
decide the bar for some symbols against the others a bar stale, which a
backtest never does.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
from librae import Backtest, FeatureBatch, MarketDataSubscription
from librae.brokers.base import settled_available_at, with_settled_available_at
from librae.core.cost_model import CostModel
from librae.core.run_config import ExecutionPolicy
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.core.trading_calendar import next_session_open, session_label
from librae.live.engine import LiveTrader

from tests.conftest import make_test_cfg

_FREQ = {"M1": "min", "M5": "5min", "H1": "h"}
BAR = pd.Timestamp("2026-01-01T02:00Z")
CLOSE = (BAR + pd.Timedelta(hours=1)).to_pydatetime()


class _Hold(Strategy):
    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        return []


def _bars(through: pd.Timestamp, start: str, freq: str) -> pd.DataFrame:
    ts = pd.date_range(start, through, freq=freq)
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
    timeframe: str = "H1",
    poll_seconds: int = 0,
    session_mode: str = "extended",
    adapters: dict | None = None,
    strategy: Strategy | None = None,
    plain: bool = False,
):
    """A sim of A and B; with ``plain``, through ``feature_fn`` rather than batch features."""
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
        strategy or _Hold(),
        (lambda frame: frame) if plain else None,
        config=make_test_cfg(
            mode="sim",
            symbols=symbols,
            timeframe=timeframe,
            poll_seconds=poll_seconds,
            session_mode=session_mode,
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
        batch_feature_fn=None if plain else features,
        adapter=adapters
        or {
            symbol: (
                lambda *_args, symbol=symbol, **_kwargs: _bars(
                    newest[symbol], start, _FREQ[timeframe]
                )
            )
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
    """B trades XNYS regular hours, closed from Friday 21:00 UTC to Monday's open."""
    friday = pd.Timestamp("2026-01-02T20:55Z")
    newest = {"A": friday, "B": friday}
    clock = {"now": (friday + pd.Timedelta(minutes=5, seconds=5)).to_pydatetime()}
    decided: list = []
    trader = _trader(
        newest,
        clock,
        decided,
        calendars={"A": "24/7", "B": "XNYS"},
        start="2026-01-02T20:30Z",
        timeframe="M5",
        session_mode="regular",
    )
    trader._poll_cycle()

    newest["A"] = friday + pd.Timedelta(minutes=5)
    clock["now"] = (friday + pd.Timedelta(minutes=10, seconds=5)).to_pydatetime()
    trader._poll_cycle()

    assert decided[-1] == (friday + pd.Timedelta(minutes=5), ["A"])


def test_nothing_is_held_when_the_next_poll_comes_after_the_next_bar_closes():
    """Live keeps only each symbol's newest bar, so with minute bars and
    55-second polls a bar held now would be skipped at the next poll."""
    bar = pd.Timestamp("2026-01-01T02:00Z")
    newest = {"A": bar - pd.Timedelta(minutes=1), "B": bar - pd.Timedelta(minutes=1)}
    clock = {"now": (bar + pd.Timedelta(seconds=30)).to_pydatetime()}
    decided: list = []
    trader = _trader(
        newest, clock, decided, start="2026-01-01T01:57Z", timeframe="M1", poll_seconds=55
    )
    trader._poll_cycle()

    newest["A"] = bar
    clock["now"] = (bar + pd.Timedelta(minutes=1, seconds=5)).to_pydatetime()
    trader._poll_cycle()

    assert decided[-1] == (bar, ["A"])


class _TaifexSource:
    """A source stamping the segment-end settle into ``available_at`` and
    declaring it, as the Shioaji adapter does; it serves a bar only once it
    is available, as ``drop_incomplete_ohlcv`` does."""

    def __init__(self, newest: dict, symbol: str, clock: dict) -> None:
        self._newest, self._symbol, self._clock = newest, symbol, clock

    def __call__(self, *_args, **_kwargs) -> pd.DataFrame:
        bars = _bars(self._newest[self._symbol], "2026-01-05T05:40Z", "min")
        bars = with_settled_available_at(bars, "1m", "XTAIFEX")
        return bars.loc[bars["available_at"] <= self._clock["now"]].reset_index(drop=True)

    def market_data_available_at(self, ts, timeframe, *, calendar_id, session_mode):
        return settled_available_at(ts, timeframe, calendar_id)


def _taifex_close(
    newest: dict,
    clock: dict,
    decided: list,
    strategy: Strategy | None = None,
    plain: bool = False,
):
    """A trades 24/7; B is a TAIFEX future whose day session closes at 05:45
    UTC, so its 05:44 minute is available a settle later than A's."""
    trader = _trader(
        newest,
        clock,
        decided,
        calendars={"A": "24/7", "B": "XTAIFEX"},
        start="2026-01-05T05:40Z",
        timeframe="M1",
        adapters={
            "A": lambda *_a, **_k: _bars(newest["A"], "2026-01-05T05:40Z", "min"),
            "B": _TaifexSource(newest, "B", clock),
        },
        strategy=strategy,
        plain=plain,
    )
    trader._poll_cycle()
    return trader


def test_a_segment_ending_bar_is_its_own_later_event():
    """B's 05:44 minute is not available when A's is, so A goes ahead at
    once rather than wait for it, and B's bar is decided when it is
    available, reading A's."""
    bar = pd.Timestamp("2026-01-05T05:44Z")
    newest = {"A": bar - pd.Timedelta(minutes=1), "B": bar - pd.Timedelta(minutes=1)}
    clock = {"now": (bar + pd.Timedelta(seconds=5)).to_pydatetime()}
    decided: list = []
    trader = _taifex_close(newest, clock, decided)

    newest["A"] = newest["B"] = bar
    clock["now"] = (bar + pd.Timedelta(minutes=1, seconds=5)).to_pydatetime()
    trader._poll_cycle()
    assert decided[-1] == (bar, ["A"])

    newest["A"] = bar + pd.Timedelta(minutes=1)
    clock["now"] = (bar + pd.Timedelta(minutes=2, seconds=5)).to_pydatetime()
    trader._poll_cycle()
    assert decided[-2:] == [(bar, ["B"]), (bar + pd.Timedelta(minutes=1), ["A"])]


class _BuyA(Strategy):
    """Buys A on a bar from ``since`` through ``until`` while A is flat,
    recording each decision."""

    def __init__(self, since: pd.Timestamp, until: pd.Timestamp | None = None) -> None:
        self._since, self._until = since, until
        self.decisions: list[tuple[pd.Timestamp, list[str]]] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        ts = pd.Timestamp(ctx.ts)
        self.decisions.append((ts, sorted(ctx.bars)))
        if (
            self._since <= ts
            and (self._until is None or ts <= self._until)
            and "A" in ctx.bars
            and "A" not in ctx.positions
        ):
            return [OrderIntent(symbol="A", action="long", quantity=1.0)]
        return []

    def last_decisions(self, since: pd.Timestamp) -> list[tuple[pd.Timestamp, list[str]]]:
        """Each event's last decision from ``since`` on, in event order."""
        last = {ts: bars for ts, bars in self.decisions if ts >= since}
        return sorted(last.items())


def _backtest_opens(
    frames: dict[str, pd.DataFrame], calendars: dict[str, str], timeframe: str, strategy: Strategy
) -> list[pd.Timestamp]:
    frame = pd.concat([bars.assign(symbol=symbol) for symbol, bars in frames.items()]).rename(
        columns={"ts": "datetime"}
    )
    result = Backtest(
        frame.set_index(["symbol", "datetime"]),
        strategy,
        strategy_name="cohort",
        cost_model=CostModel.zero(),
        primary_subscriptions=[
            MarketDataSubscription(
                symbol=symbol,
                timeframe=timeframe,
                calendar_id=calendar_id,
                session_mode="extended",
                data_source="fixture",
                instrument_type="spot",
            )
            for symbol, calendar_id in calendars.items()
        ],
    ).run()
    return [
        pd.Timestamp(event.ts) for event in result.position_events if event.event_type == "open"
    ]


_TAIFEX_START = "2026-01-05T05:40Z"


def _taifex_bars(through: pd.Timestamp, b_through: pd.Timestamp) -> dict[str, pd.DataFrame]:
    return {
        "A": _bars(through, _TAIFEX_START, "min"),
        "B": with_settled_available_at(_bars(b_through, _TAIFEX_START, "min"), "1m", "XTAIFEX"),
    }


def test_a_decision_on_a_settled_bar_fills_on_a_bar_opening_after_it():
    """Polled after B's settle, the 05:44 bars are decided together at
    05:46, after A's 05:45 minute opened: A fills on its 05:46 minute, as a
    backtest of the same bars does."""
    bar = pd.Timestamp("2026-01-05T05:44Z")
    newest = {"A": bar - pd.Timedelta(minutes=1), "B": bar - pd.Timedelta(minutes=1)}
    clock = {"now": (bar + pd.Timedelta(seconds=5)).to_pydatetime()}
    decided: list = []
    trader = _taifex_close(newest, clock, decided, strategy=_BuyA(bar, bar))
    fills: list = []
    trader._on_position_event = lambda event, _sequence: fills.append(pd.Timestamp(event.ts))

    newest["A"], newest["B"] = bar + pd.Timedelta(minutes=1), bar
    clock["now"] = (bar + pd.Timedelta(minutes=2, seconds=5)).to_pydatetime()
    trader._poll_cycle()
    assert decided[-2:] == [(bar, ["A", "B"]), (bar + pd.Timedelta(minutes=1), ["A"])]
    assert fills == []

    newest["A"] = bar + pd.Timedelta(minutes=2)
    clock["now"] = (bar + pd.Timedelta(minutes=3, seconds=5)).to_pydatetime()
    trader._poll_cycle()
    assert fills == [bar + pd.Timedelta(minutes=2)]
    assert fills == _backtest_opens(
        _taifex_bars(bar + pd.Timedelta(minutes=2), bar),
        {"A": "24/7", "B": "XTAIFEX"},
        "M1",
        _BuyA(bar, bar),
    )


def test_a_settled_bar_decided_again_replaces_the_decision_made_without_it():
    """Polled before B's settle, A's 05:44 minute is decided alone; once B's
    arrives, sim decides 05:44 again with both, as a backtest decides it
    once. That decision replaces the first: no duplicate intent, and A fills
    on its 05:46 minute, as in the backtest."""
    bar = pd.Timestamp("2026-01-05T05:44Z")
    newest = {"A": bar - pd.Timedelta(minutes=1), "B": bar - pd.Timedelta(minutes=1)}
    clock = {"now": (bar + pd.Timedelta(seconds=5)).to_pydatetime()}
    strategy = _BuyA(bar, bar)
    trader = _taifex_close(newest, clock, [], strategy=strategy, plain=True)
    fills: list = []
    trader._on_position_event = lambda event, _sequence: fills.append(pd.Timestamp(event.ts))
    for minutes in range(1, 4):
        newest["A"], newest["B"] = bar + pd.Timedelta(minutes=minutes - 1), bar
        clock["now"] = (bar + pd.Timedelta(minutes=minutes, seconds=5)).to_pydatetime()
        trader._poll_cycle()

    assert strategy.decisions[1:3] == [(bar, ["A"]), (bar, ["A", "B"])]
    assert not trader._halted
    backtest = _BuyA(bar, bar)
    assert fills == [bar + pd.Timedelta(minutes=2)]
    assert fills == _backtest_opens(
        _taifex_bars(bar + pd.Timedelta(minutes=2), bar),
        {"A": "24/7", "B": "XTAIFEX"},
        "M1",
        backtest,
    )
    assert strategy.last_decisions(bar) == backtest.last_decisions(bar)


def test_a_bar_decided_again_with_a_late_symbol_replaces_the_earlier_decision():
    """B's 02:00 bar arrives past the hold, after A's was decided alone. Sim
    decides 02:00 again with both and the second decision replaces the
    first: a strategy buying A whenever it is flat buys it once, on the
    03:00 bar, as a backtest does."""
    newest = {"A": BAR - pd.Timedelta(hours=1), "B": BAR - pd.Timedelta(hours=1)}
    clock = {"now": CLOSE - timedelta(minutes=30)}
    strategy = _BuyA(BAR)
    trader = _trader(newest, clock, [], strategy=strategy, plain=True)
    fills: list = []
    trader._on_position_event = lambda event, _sequence: fills.append(
        (pd.Timestamp(event.ts), event.event_type)
    )
    trader._poll_cycle()
    newest["A"] = BAR
    clock["now"] = CLOSE + LiveTrader.COHORT_HOLD
    trader._poll_cycle()
    newest["B"] = BAR
    clock["now"] = CLOSE + LiveTrader.COHORT_HOLD + timedelta(seconds=30)
    trader._poll_cycle()
    for hours in (1, 2):
        newest["A"] = newest["B"] = BAR + pd.Timedelta(hours=hours)
        clock["now"] = CLOSE + timedelta(hours=hours, seconds=5)
        trader._poll_cycle()

    assert strategy.decisions[1:3] == [(BAR, ["A"]), (BAR, ["A", "B"])]
    assert not trader._halted
    backtest = _BuyA(BAR)
    assert [ts for ts, event_type in fills if event_type == "open"] == _backtest_opens(
        {symbol: _bars(BAR + pd.Timedelta(hours=2), "2026-01-01T00:00Z", "h") for symbol in "AB"},
        {"A": "24/7", "B": "24/7"},
        "H1",
        backtest,
    )
    assert fills == [(BAR + pd.Timedelta(hours=1), "open")]
    assert strategy.last_decisions(BAR) == backtest.last_decisions(BAR)


def test_a_bar_delivered_past_the_hold_is_decided_late_rather_than_refused():
    """B's 05:44 minute is due by A's 05:45 event but arrives past the hold."""
    bar = pd.Timestamp("2026-01-05T05:44Z")
    newest = {"A": bar - pd.Timedelta(minutes=1), "B": bar - pd.Timedelta(minutes=1)}
    clock = {"now": (bar + pd.Timedelta(seconds=5)).to_pydatetime()}
    decided: list = []
    trader = _taifex_close(newest, clock, decided)

    newest["A"] = bar + pd.Timedelta(minutes=1)
    for seconds in (5, 35):
        clock["now"] = (bar + pd.Timedelta(minutes=2, seconds=seconds)).to_pydatetime()
        trader._poll_cycle()
    assert decided[-1] == (bar + pd.Timedelta(minutes=1), ["A"])

    newest["B"] = bar
    clock["now"] = (bar + pd.Timedelta(minutes=2, seconds=40)).to_pydatetime()
    trader._poll_cycle()
    assert decided[-1] == (bar, ["B"])


class _IbkrExtendedDaily:
    """Extended-session XNYS daily bars usable from the next session open,
    declared as the IBKR adapter declares them."""

    def __init__(self, newest: dict) -> None:
        self._newest = newest

    def __call__(self, *_args, **_kwargs) -> pd.DataFrame:
        return _xnys_daily(self._newest["B"], available="next_open")

    def market_data_available_at(self, ts, timeframe, *, calendar_id, session_mode):
        return next_session_open(session_label(ts, calendar_id), calendar_id)


def _xnys_daily(through: pd.Timestamp, *, available: str) -> pd.DataFrame:
    opens = pd.DatetimeIndex(
        ["2026-01-05T14:30Z", "2026-01-06T14:30Z", "2026-01-07T14:30Z", "2026-01-08T14:30Z"]
    )
    frame = pd.DataFrame(
        {
            "ts": opens[:-1],
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "close": 1.0,
            "volume": 1.0,
            "available_at": opens[1:] if available == "next_open" else opens[:-1] + _SESSION,
        }
    )
    return frame.loc[frame["ts"] <= through].reset_index(drop=True)


_SESSION = pd.Timedelta(hours=6, minutes=30)


def test_a_symbol_published_after_its_close_does_not_hold_the_rest():
    """Both trade XNYS daily bars. A's source publishes Wednesday's at the
    close; B's only at Thursday's open, so B is not due A's event."""
    wednesday = pd.Timestamp("2026-01-07T14:30Z")
    newest = {"A": wednesday - pd.Timedelta(days=1), "B": wednesday - pd.Timedelta(days=1)}
    clock = {"now": pd.Timestamp("2026-01-07T15:00Z").to_pydatetime()}
    decided: list = []
    trader = _trader(
        newest,
        clock,
        decided,
        calendars={"A": "XNYS", "B": "XNYS"},
        timeframe="D1",
        adapters={
            "A": lambda *_a, **_k: _xnys_daily(newest["A"], available="close"),
            "B": _IbkrExtendedDaily(newest),
        },
    )
    trader._poll_cycle()

    newest["A"] = wednesday
    clock["now"] = (wednesday + _SESSION + pd.Timedelta(seconds=5)).to_pydatetime()
    trader._poll_cycle()

    assert decided[-1] == (wednesday, ["A"])
