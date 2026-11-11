"""Live Shioaji polls served from the tick subscription (librae.brokers.shioaji_stream).

A fake session keeps the exchange's trades: it delivers them to the tick
callback as the subscription would, and answers kbars() with the minutes
they make, so a test can drop a delivery and check what the poll serves.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
import pytest
from librae.brokers.shioaji_adapter import ShioajiAdapter
from librae.brokers.shioaji_stream import ShioajiBarStream

_TAIPEI = "Asia/Taipei"


def _taipei(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz=_TAIPEI).tz_convert("UTC")


class FakeSession:
    """The Shioaji calls the bar stream makes, over a list of exchange trades."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.contract = SimpleNamespace(security_type="FUT", code="TXFR1", target_code="TXFK6")
        self.contracts = SimpleNamespace(get=lambda _symbol: self.contract)
        self.trades: list[tuple[str, datetime, float, int]] = []
        self.totals: dict[str, int] = defaultdict(int)
        self.subscribed: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.kbars_calls: list[tuple[str, str]] = []
        self.kbars_answer_empty = False
        self.kbars_lag = pd.Timedelta(0)  # how far behind the trades its answers run
        # TAIFEX's TMF kbars() give the trades matched at a segment close a
        # minute of their own, stamped one past the close; TXF's fold them in.
        self.kbars_close_apart = False
        self.on_tick = self.on_event = self.on_session_down = None

    def set_on_tick_fop_v1_callback(self, callback):
        self.on_tick = callback

    def set_event_callback(self, callback):
        self.on_event = callback

    def set_session_down_callback(self, callback):
        self.on_session_down = callback

    def subscribe(self, contract, quote_type):
        assert quote_type == "tick"
        self.calls.append(("subscribe", contract.target_code or contract.code))
        self.subscribed.add(contract.target_code or contract.code)

    def unsubscribe(self, contract, quote_type):
        assert quote_type == "tick"
        self.calls.append(("unsubscribe", contract.target_code or contract.code))
        self.subscribed.discard(contract.target_code or contract.code)

    def trade(
        self,
        at: str,
        price: float,
        volume: int = 1,
        *,
        code: str = "TXFK6",
        delivered: bool = True,
        total: int | None = None,
        simtrade: bool = False,
    ) -> None:
        wall = pd.Timestamp(at).to_pydatetime()
        if not simtrade:
            self.trades.append((code, wall, price, volume))
            self.totals[code] += volume
        if delivered and code in self.subscribed:
            tick = SimpleNamespace(
                code=code,
                datetime=wall,
                close=str(price),
                volume=volume,
                total_volume=self.totals[code] if total is None else total,
                simtrade=simtrade,
            )
            self.on_tick(tick)

    def kbars(self, *, contract, start, end):
        self.kbars_calls.append((start, end))
        code = contract.target_code or contract.code
        now = self.clock.now.tz_convert(_TAIPEI).tz_localize(None) - self.kbars_lag
        rows: dict[pd.Timestamp, list] = {}
        for trade_code, wall, price, volume in self.trades:
            if trade_code != code or wall >= now:
                continue
            stamp = pd.Timestamp(wall).floor("min") + pd.Timedelta(minutes=1)
            # The closing auction prints just after 13:45 into the last minute.
            if not self.kbars_close_apart and stamp.time() == pd.Timestamp("13:46").time():
                stamp -= pd.Timedelta(minutes=1)
            if not start <= stamp.strftime("%Y-%m-%d") <= end:
                continue
            row = rows.setdefault(stamp, [price, price, price, price, 0])
            row[1], row[2], row[3] = max(row[1], price), min(row[2], price), price
            row[4] += volume
        if self.kbars_answer_empty:
            rows = {}
        stamps = sorted(rows)
        return {
            "ts": [stamp.tz_localize("UTC").value for stamp in stamps],
            "Open": [rows[s][0] for s in stamps],
            "High": [rows[s][1] for s in stamps],
            "Low": [rows[s][2] for s in stamps],
            "Close": [rows[s][3] for s in stamps],
            "Volume": [rows[s][4] for s in stamps],
        }


class Clock:
    def __init__(self, monkeypatch) -> None:
        self.now = _taipei("2026-11-05 09:00")
        clock = self
        monkeypatch.setattr(
            pd.Timestamp, "now", classmethod(lambda _cls, tz=None: clock.now.tz_convert(tz))
        )
        datetime_patch = patch("librae.brokers.base.datetime")
        self._base_datetime = datetime_patch.start()
        self._base_datetime.now.side_effect = lambda tz=None: clock.now.to_pydatetime()
        self._stop = datetime_patch.stop

    def at(self, value: str) -> None:
        self.now = _taipei(value)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock(monkeypatch)
    yield clock
    clock._stop()


@pytest.fixture
def session(clock):
    return FakeSession(clock)


@pytest.fixture
def adapter(session):
    adapter = ShioajiAdapter.__new__(ShioajiAdapter)
    adapter._api = session
    adapter._read_only = True
    adapter._bar_stream = ShioajiBarStream(session, adapter._kbars_minutes)
    return adapter


def _poll(adapter, clock, at: str, timeframe: str = "1m", limit: int = 1000) -> pd.DataFrame:
    """A live poll at ``at``, indexed by Taipei bar start for readability."""
    clock.at(at)
    bars = adapter.fetch_ohlcv(
        "TXFR1", timeframe, limit=limit, continuous_alias=True, drop_incomplete=True
    )
    bars.index = (
        pd.to_datetime(bars.pop("ts"), utc=True).dt.tz_convert(_TAIPEI).dt.strftime("%H:%M")
    )
    return bars.drop(columns="available_at")


def _exchange_minutes(adapter, clock, at: str, timeframe: str = "1m") -> pd.DataFrame:
    """What a dated kbars() fetch of today returns, indexed like _poll."""
    clock.at(at)
    bars = adapter.fetch_ohlcv("TXFR1", timeframe, start="2026-11-05", continuous_alias=True)
    bars.index = (
        pd.to_datetime(bars.pop("ts"), utc=True).dt.tz_convert(_TAIPEI).dt.strftime("%H:%M")
    )
    return bars.drop(columns="available_at")


def _trade_each_minute(session, first: str, last: str, *, second: int = 20, **kwargs) -> None:
    for minute in pd.date_range(first, last, freq="min"):
        session.trade(str(minute + pd.Timedelta(seconds=second)), 100 + minute.minute, 1, **kwargs)
        session.trade(
            str(minute + pd.Timedelta(seconds=second + 30)), 99 + minute.minute, 2, **kwargs
        )


class TestSteadyPolling:
    def test_polls_after_startup_ask_kbars_only_to_seed_and_settle_it(
        self, adapter, session, clock
    ):
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 09:00")
        startup = _poll(adapter, clock, "2026-11-05 09:01:05")
        assert len(session.kbars_calls) == 1
        assert startup.index[-1] == "08:59", "09:00 waits until kbars() settles it"

        polled = None
        for minute in pd.date_range("2026-11-05 09:01", "2026-11-05 09:30", freq="min"):
            _trade_each_minute(session, str(minute), str(minute))
            polled = _poll(adapter, clock, str(minute + pd.Timedelta(seconds=55)))

        assert len(session.kbars_calls) == 2, "seed, then one refill once the stream took over"
        assert session.kbars_calls[0] == ("2026-11-04", "2026-11-05")
        assert polled.index[-1] == "09:29", "09:30 is still forming"
        expected = _exchange_minutes(adapter, clock, "2026-11-05 09:40").loc[:"09:29"]
        pd.testing.assert_frame_equal(polled, expected, check_dtype=False)

    def test_a_coarser_bar_completes_from_streamed_minutes(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:44:00")
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 09:00")

        polled = _poll(adapter, clock, "2026-11-05 09:00:55", "5m")

        assert len(session.kbars_calls) == 1, "nothing traded between the seed and the stream"
        expected = _exchange_minutes(adapter, clock, "2026-11-05 09:10", "5m")
        pd.testing.assert_frame_equal(
            polled, expected.loc[["08:45", "08:50", "08:55"]], check_dtype=False
        )

    def test_a_daily_poll_is_served_from_the_stream(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:44:00", "1d", limit=2)
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 13:44")

        polled = _poll(adapter, clock, "2026-11-05 13:46:01", "1d", limit=2)

        assert len(session.kbars_calls) == 2, "the seed, and the day session's end"
        assert polled.loc["15:00", "volume"] == 3 * 300


class TestMinuteRule:
    def test_trades_count_from_their_minute_start_and_the_close_joins_the_last(
        self, adapter, session, clock
    ):
        _poll(adapter, clock, "2026-11-05 13:38:30")
        _poll(adapter, clock, "2026-11-05 13:40:05")  # settles what came before the stream
        session.trade("2026-11-05 13:41:00.000", 100, 1)
        session.trade("2026-11-05 13:41:59.999", 101, 2)
        session.trade("2026-11-05 13:42:00.000", 102, 3)
        session.trade("2026-11-05 13:44:59.990", 103, 4)
        session.trade("2026-11-05 13:45:00.024", 104, 5)

        clock.at("2026-11-05 13:45:30")
        streamed = (
            adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)
            .set_index("ts")
            .drop(columns="available_at")
        )
        polled = _poll(adapter, clock, "2026-11-05 13:46:01")

        assert streamed.loc[_taipei("2026-11-05 13:41")].tolist() == [100, 101, 100, 101, 3]
        assert streamed.loc[_taipei("2026-11-05 13:42")].tolist() == [102, 102, 102, 102, 3]
        assert streamed.loc[_taipei("2026-11-05 13:44")].tolist() == [103, 104, 103, 104, 9]
        assert len(session.kbars_calls) == 3, "the day session's end is settled by kbars()"
        expected = _exchange_minutes(adapter, clock, "2026-11-05 13:50")
        pd.testing.assert_frame_equal(polled, expected, check_dtype=False)

    def test_a_closing_print_kbars_stamps_apart_joins_the_last_minute(
        self, adapter, session, clock
    ):
        session.kbars_close_apart = True
        _poll(adapter, clock, "2026-11-05 08:44:00", "1d", limit=2)
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 13:44")
        session.trade("2026-11-05 13:45:00.024", 150, 5)

        days = _poll(adapter, clock, "2026-11-05 13:46:01", "1d", limit=2)
        minutes = _exchange_minutes(adapter, clock, "2026-11-05 13:50")

        assert (days.loc["15:00", "close"], days.loc["15:00", "volume"]) == (150, 3 * 300 + 5)
        assert minutes.index[-1] == "13:44"
        assert minutes.loc["13:44", ["close", "volume"]].tolist() == [150, 3 + 5]

    def test_a_closing_print_as_the_first_tick_seen_joins_the_last_minute(self):
        from librae.brokers.shioaji_stream import _MinuteBook, _tick_ns

        book = _MinuteBook("TXFK6", "XTAIFEX")
        at = _tick_ns(datetime(2026, 11, 5, 13, 45, 0, 24000))

        assert book._minute_of(at) == (_taipei("2026-11-05 13:44").value, True)

    def test_trial_matching_is_not_a_trade(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:30:00")
        session.trade("2026-11-05 08:44:50", 90, 7, simtrade=True)
        session.trade("2026-11-05 08:45:00.100", 100, 1)

        polled = _poll(adapter, clock, "2026-11-05 08:47:00")

        assert polled.loc["08:45"].tolist() == [100, 100, 100, 100, 1]

    def test_a_tick_delivered_twice_counts_once(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:30:00")
        session.trade("2026-11-05 08:45:10", 100, 2)
        session.on_tick(
            "TAIFEX",  # the call shape of Shioaji's streaming tutorial
            SimpleNamespace(
                code="TXFK6",
                datetime=pd.Timestamp("2026-11-05 08:45:10").to_pydatetime(),
                close="100",
                volume=2,
                total_volume=2,
                simtrade=False,
            ),
        )

        polled = _poll(adapter, clock, "2026-11-05 08:47:00")

        assert polled.loc["08:45", "volume"] == 2
        assert len(session.kbars_calls) == 1


class TestGaps:
    def _streaming(self, adapter, session, clock) -> None:
        """Seeded before the open, so the stream holds the session whole."""
        _poll(adapter, clock, "2026-11-05 08:30:00")
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 09:04")
        assert _poll(adapter, clock, "2026-11-05 09:04:55").index[-1] == "09:03"

    def test_a_missed_tick_holds_its_minutes_until_kbars_settles_them(
        self, adapter, session, clock
    ):
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:10", 120, 4)
        session.trade("2026-11-05 09:05:30", 121, 5, delivered=False)
        session.trade("2026-11-05 09:05:50", 122, 6)
        _trade_each_minute(session, "2026-11-05 09:06", "2026-11-05 09:06")

        held = _poll(adapter, clock, "2026-11-05 09:06:55")
        assert held.index[-1] == "09:04", "09:05 lacks a trade"
        assert len(session.kbars_calls) == 1

        _trade_each_minute(session, "2026-11-05 09:07", "2026-11-05 09:08")
        refilled = _poll(adapter, clock, "2026-11-05 09:08:55")

        assert len(session.kbars_calls) == 2
        assert refilled.loc["09:05"].tolist() == [120, 122, 120, 122, 15]
        assert refilled.index[-1] == "09:07"

    def test_a_poll_keeping_incomplete_bars_sees_the_held_minutes_as_streamed(
        self, adapter, session, clock
    ):
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:10", 120, 4)
        session.trade("2026-11-05 09:05:30", 121, 5, delivered=False)
        session.trade("2026-11-05 09:05:50", 122, 6)

        clock.at("2026-11-05 09:05:55")
        bars = adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)

        newest = bars.iloc[-1]
        assert newest["ts"] == _taipei("2026-11-05 09:05")
        assert newest["volume"] == 10, "the streamed ticks, short of the one missed"
        assert len(session.kbars_calls) == 1

    def test_a_coarser_bar_with_a_held_minute_is_held_after_its_close(
        self, adapter, session, clock
    ):
        self._streaming(adapter, session, clock)
        _trade_each_minute(session, "2026-11-05 09:05", "2026-11-05 09:06")
        session.trade("2026-11-05 09:07:10", 120, 4, delivered=False)
        _trade_each_minute(session, "2026-11-05 09:07", "2026-11-05 09:11")
        # A refill just happened, so the next one waits five minutes.
        adapter._bar_stream._books["TXFR1"].last_refill_ns = _taipei("2026-11-05 09:08").value

        held = _poll(adapter, clock, "2026-11-05 09:11:55", "5m")

        assert held.index[-1] == "09:00", "the 09:05 bar holds 09:07, which lacks a trade"
        assert len(session.kbars_calls) == 1

    def test_kbars_settles_a_minute_the_stream_counted_twice(self, adapter, session, clock):
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:50", 120, 4)
        replay = SimpleNamespace(
            code="TXFK6",
            datetime=pd.Timestamp("2026-11-05 09:05:50").to_pydatetime(),
            close="120",
            volume=4,
            total_volume=session.totals["TXFK6"],
            simtrade=False,
        )
        session.on_event(0, 12, "", "Session reconnecting")
        _poll(adapter, clock, "2026-11-05 09:06:05")  # renews the subscription
        session.on_tick(replay)  # had the renewed subscription sent it again
        _trade_each_minute(session, "2026-11-05 09:06", "2026-11-05 09:08")

        polled = _poll(adapter, clock, "2026-11-05 09:08:05")

        assert len(session.kbars_calls) == 2
        assert polled.loc["09:05", "volume"] == 4
        assert polled.index[-1] == "09:07"

    def test_a_lost_connection_renews_the_subscription_and_settles_by_kbars(
        self, adapter, session, clock
    ):
        self._streaming(adapter, session, clock)
        session.on_event(0, 12, "", "Session reconnecting")
        session.trade("2026-11-05 09:05:10", 120, 4, delivered=False)

        held = _poll(adapter, clock, "2026-11-05 09:05:30")
        assert session.calls[-2:] == [("unsubscribe", "TXFK6"), ("subscribe", "TXFK6")]
        assert held.index[-1] == "09:03", "09:04 may have lost trades after its last tick"

        _trade_each_minute(session, "2026-11-05 09:06", "2026-11-05 09:08")
        refilled = _poll(adapter, clock, "2026-11-05 09:08:55")

        assert len(session.kbars_calls) == 2
        assert refilled.loc["09:05", "volume"] == 4
        expected = _exchange_minutes(adapter, clock, "2026-11-05 09:20").loc[:"09:07"]
        pd.testing.assert_frame_equal(refilled, expected, check_dtype=False)

    def test_an_empty_refill_leaves_the_minutes_held(self, adapter, session, clock):
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:10", 120, 4, delivered=False)
        _trade_each_minute(session, "2026-11-05 09:05", "2026-11-05 09:07")
        session.kbars_answer_empty = True

        held = _poll(adapter, clock, "2026-11-05 09:07:55")

        assert len(session.kbars_calls) == 2
        assert held.index[-1] == "09:03", "09:04's last tick may not have been its last trade"

    @pytest.mark.parametrize(
        ("lag", "refill_at"),
        [("3min", "09:08:55"), ("2min", "09:07:05")],
        ids=["lacks-a-later-minute", "lacks-the-gap-minute"],
    )
    def test_a_late_kbars_answer_does_not_settle_what_it_lacks(
        self, adapter, session, clock, lag, refill_at
    ):
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:10", 120, 4)
        session.trade("2026-11-05 09:05:30", 121, 5, delivered=False)
        session.trade("2026-11-05 09:05:50", 122, 6)
        _trade_each_minute(session, "2026-11-05 09:06", "2026-11-05 09:13")
        session.kbars_lag = pd.Timedelta(lag)

        late = _poll(adapter, clock, f"2026-11-05 {refill_at}")
        session.kbars_lag = pd.Timedelta(0)
        caught_up = _poll(adapter, clock, "2026-11-05 09:13:55")

        assert late.index[-1] == "09:04", "09:05 waits for an answer that has it"
        assert len(session.kbars_calls) == 3
        assert caught_up.loc["09:05"].tolist() == [120, 122, 120, 122, 15]
        assert caught_up.index[-1] == "09:12"

    def test_a_failed_seed_is_retried_after_a_pause(self, adapter, session, clock, monkeypatch):
        answers = iter([RuntimeError("timeout")])

        def kbars(**query):
            error = next(answers, None)
            if error is not None:
                raise error
            return FakeSession.kbars(session, **query)

        monkeypatch.setattr(session, "kbars", kbars)
        with pytest.raises(RuntimeError, match="timeout"):
            _poll(adapter, clock, "2026-11-05 09:00:00")
        with pytest.raises(RuntimeError, match="asked again"):
            _poll(adapter, clock, "2026-11-05 09:01:00")
        _poll(adapter, clock, "2026-11-05 09:05:00")

        assert len(session.kbars_calls) == 1, "the failed query records no answer"

    def test_refills_stop_at_the_daily_budget(self, adapter, session, clock, monkeypatch):
        monkeypatch.setattr("librae.brokers.shioaji_stream._DAILY_KBARS_BUDGET", 1)
        self._streaming(adapter, session, clock)
        session.trade("2026-11-05 09:05:10", 120, 4, delivered=False)
        _trade_each_minute(session, "2026-11-05 09:05", "2026-11-05 09:07")

        with pytest.raises(RuntimeError, match="kbars\\(\\) queries"):
            _poll(adapter, clock, "2026-11-05 09:07:55")


class TestSegments:
    def test_a_missed_closing_print_is_settled_by_kbars(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:30:00")
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 13:44")
        session.trade("2026-11-05 13:45:00.024", 150, 500, delivered=False)
        session.trade("2026-11-05 15:00:00.100", 151, 2, total=2)  # the night restarts

        minutes = _poll(adapter, clock, "2026-11-05 15:00:30")
        daily = _poll(adapter, clock, "2026-11-05 15:00:31", "1d", limit=2)

        assert minutes.loc["13:44"].tolist() == [144, 150, 143, 150, 503]
        assert daily.loc["15:00", ["close", "volume"]].tolist() == [150, 3 * 300 + 500]

    def test_a_late_answer_at_the_segment_end_keeps_the_streamed_close(
        self, adapter, session, clock
    ):
        _poll(adapter, clock, "2026-11-05 08:30:00", "1d", limit=2)
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 13:44")
        session.trade("2026-11-05 13:45:00.024", 150, 500)
        session.kbars_lag = pd.Timedelta(minutes=3)

        held = _poll(adapter, clock, "2026-11-05 13:46:30", "1d", limit=2)
        session.kbars_lag = pd.Timedelta(0)
        settled = _poll(adapter, clock, "2026-11-05 13:51:31", "1d", limit=2)

        assert "15:00" not in held.index
        assert settled.loc["15:00", ["close", "volume"]].tolist() == [150, 3 * 300 + 500]
        assert len(session.kbars_calls) == 3

    def test_the_day_close_is_refilled_once_after_its_settle(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:30:00")
        _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 13:44")
        session.trade("2026-11-05 13:45:00.024", 150, 500)

        closing = _poll(adapter, clock, "2026-11-05 13:45:30")
        assert len(session.kbars_calls) == 1, "the settle has not passed"
        assert closing.index[-1] == "13:43", "13:44 waits for kbars()"

        refilled = _poll(adapter, clock, "2026-11-05 13:46:05")
        for later in ("2026-11-05 13:51:00", "2026-11-05 14:30:00"):
            _poll(adapter, clock, later)

        assert refilled.index[-1] == "13:44"
        assert refilled.loc["13:44"].tolist() == [144, 150, 143, 150, 503]
        assert session.kbars_calls == [("2026-11-04", "2026-11-05")] * 2, (
            "one refill, asking only the day the stream took over"
        )

    def test_the_segment_end_is_held_until_kbars_settles_it(self, adapter, session, clock):
        _poll(adapter, clock, "2026-11-05 08:30:00")
        _trade_each_minute(session, "2026-11-05 13:40", "2026-11-05 13:44")
        # Asked just before: the next refill waits five minutes.
        adapter._bar_stream._books["TXFR1"].last_refill_ns = _taipei("2026-11-05 13:44").value

        held = _poll(adapter, clock, "2026-11-05 13:47:00")
        settled = _poll(adapter, clock, "2026-11-05 13:49:00")

        assert held.index[-1] == "13:43"
        assert settled.index[-1] == "13:44"
        assert len(session.kbars_calls) == 2

    @staticmethod
    def _night_streamed(adapter, session, clock) -> None:
        session.trade("2026-11-05 04:57:30", 99, 1)
        _poll(adapter, clock, "2026-11-05 04:58:00")
        session.trade("2026-11-05 04:59:30", 100, 3, total=3)
        _poll(adapter, clock, "2026-11-05 05:01:30")  # settles the night's end
        assert len(session.kbars_calls) == 2

    @pytest.mark.parametrize("day_total", [2, 5], ids=["restarted", "carried"])
    def test_the_day_segment_may_restart_or_carry_the_cumulative_volume(
        self, adapter, session, clock, day_total
    ):
        self._night_streamed(adapter, session, clock)
        session.trade("2026-11-05 08:45:01", 101, 2, total=day_total)
        session.trade("2026-11-05 08:45:20", 102, 1, total=day_total + 1)

        polled = _poll(adapter, clock, "2026-11-05 08:47:00")

        assert len(session.kbars_calls) == 2
        assert polled.loc["08:45", "volume"] == 3

    def test_a_segment_opening_short_of_trades_is_a_gap(self, adapter, session, clock):
        self._night_streamed(adapter, session, clock)
        session.trade("2026-11-05 08:45:01", 101, 2, total=9)

        polled = _poll(adapter, clock, "2026-11-05 08:46:30")

        assert polled.index[-1] == "04:59", "08:45 waits for kbars()"


def test_a_rolled_alias_follows_its_next_contract(adapter, session, clock):
    _poll(adapter, clock, "2026-11-05 08:30:00")
    session.contract = SimpleNamespace(security_type="FUT", code="TXFR1", target_code="TXFL6")

    _poll(adapter, clock, "2026-11-05 08:31:00")

    assert session.calls == [
        ("subscribe", "TXFK6"),
        ("unsubscribe", "TXFK6"),
        ("subscribe", "TXFL6"),
    ]
    assert len(session.kbars_calls) == 2


def test_ticks_from_the_sdk_thread_are_not_lost(adapter, session, clock):
    _poll(adapter, clock, "2026-11-05 08:30:00")
    trades = [
        f"2026-11-05 08:{45 + i // 600:02d}:{(i // 10) % 60:02d}.{i % 10}00" for i in range(3000)
    ]
    sender = threading.Thread(target=lambda: [session.trade(at, 100.0, 1) for at in trades])

    sender.start()
    while sender.is_alive():
        _poll(adapter, clock, "2026-11-05 08:40:00")
    sender.join()
    polled = _poll(adapter, clock, "2026-11-05 08:52:00")

    assert polled["volume"].sum() == 3000
    assert len(session.kbars_calls) == 1


def test_a_dated_fetch_still_asks_kbars(adapter, session, clock):
    _trade_each_minute(session, "2026-11-05 08:45", "2026-11-05 08:50")

    _exchange_minutes(adapter, clock, "2026-11-05 09:00")

    assert session.calls == []
    assert session.kbars_calls == [("2026-11-05", "2026-11-05")]


def test_a_naive_tick_time_reads_as_taipei():
    from librae.brokers.shioaji_stream import _tick_ns

    assert _tick_ns(datetime(2026, 11, 5, 8, 45, 0, 1)) == _taipei("2026-11-05 08:45").value + 1000
