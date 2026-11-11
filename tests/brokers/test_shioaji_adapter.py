"""Tests for ShioajiAdapter.

All tests use mocks — no real Shioaji API calls.
The optional SDK contract is covered separately without a broker login.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from librae.brokers.shioaji_adapter import _require_shioaji
from librae.brokers.shioaji_time import shioaji_kbar_starts
from librae.core.cost_model import CostModel
from librae.core.market_data import MarketDataSubscription, normalize_bar_times
from librae.core.readiness import next_expected_close
from librae.core.trading_calendar import resample_session_ohlcv
from librae.live.executor import (
    LiveExecutor,
    OrderBelowVenueMinimumError,
    OrderRequest,
    PositionRequest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position_request(symbol: str) -> PositionRequest:
    return PositionRequest(
        symbol=symbol,
        venue_symbol=symbol,
        currency="TWD",
        multiplier=200.0,
        continuous_alias=symbol.endswith(("R1", "R2")),
    )


def _rolling_contract(
    code: str = "TXFR1",
    target_code: str = "TXF202608",
    **values,
):
    return SimpleNamespace(
        security_type="FUT",
        code=code,
        target_code=target_code,
        **values,
    )


def _trading_credentials(ca_path: str = "/path/to/ca"):
    from librae.brokers.shioaji_adapter import ShioajiCredentials

    return ShioajiCredentials(
        api_key="k",
        secret_key="s",
        person_id="p",
        ca_path=ca_path,
        ca_password="pw",
    )


def _mock_shioaji_actions():
    return SimpleNamespace(Action=SimpleNamespace(Buy="Buy", Sell="Sell"))


def test_missing_shioaji_names_install_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "shioaji", None)
    with pytest.raises(ImportError, match="tw-live"):
        _require_shioaji()


def test_available_symbols_lists_tmf_exact_front_and_next_contracts():
    adapter = _make_adapter()
    adapter._api.contracts.futures.return_value = [
        SimpleNamespace(
            code="TMFH6",
            target_code="",
            name="微型臺指 202608",
            delivery_month="202608",
            currency="TWD",
            unit=10,
        ),
        SimpleNamespace(
            code="TMFR1",
            target_code="TMFH6",
            name="微型臺指 近月",
            delivery_month="202608",
            currency="TWD",
            unit=10,
        ),
        SimpleNamespace(
            code="TMFI6",
            target_code="",
            name="微型臺指 202609",
            delivery_month="202609",
            currency="TWD",
            unit=10,
        ),
        SimpleNamespace(
            code="TMFR2",
            target_code="TMFI6",
            name="微型臺指 次月",
            delivery_month="202609",
            currency="TWD",
            unit=10,
        ),
    ]

    results = adapter.available_symbols(
        query="TMF",
        kind="future",
        asset_class="index",
    )

    assert {(item.native_symbol, item.contract_rank) for item in results} == {
        ("TMFH6", 0),
        ("TMFR1", 0),
        ("TMFI6", 1),
        ("TMFR2", 1),
    }
    front_alias = next(item for item in results if item.native_symbol == "TMFR1")
    assert front_alias.continuous_alias is True
    assert front_alias.contract_month is None
    assert front_alias.delivery_month == "202608"
    adapter._api.contracts.futures.assert_called_once_with("TMF")


def _make_adapter(*, ca_activated: bool = False):
    """Build a ShioajiAdapter with mocked internals (no real login)."""
    from librae.brokers.shioaji_adapter import ShioajiAdapter
    from librae.brokers.shioaji_stream import ShioajiBarStream

    adapter = ShioajiAdapter.__new__(ShioajiAdapter)
    adapter._api = MagicMock()
    adapter._read_only = not ca_activated
    adapter._bar_stream = ShioajiBarStream(adapter._api, adapter._kbars_minutes)
    return adapter


def _make_kbars_response(day: str = "2026-04-01"):
    """Simulate Shioaji kbars() return value (dict of lists).

    ``ts`` is raw nanoseconds, encoding Taipei wall-clock time as if it were
    a UTC epoch and marking the minute's end (see shioaji_time.py) — these
    raw stamps read Taipei 09:00/09:05/09:10 on ``day``, i.e. on 2026-04-01
    minutes starting at true UTC 00:59/01:04/01:09.
    """
    taipei_wall_clock = pd.to_datetime(
        [f"{day} 09:00", f"{day} 09:05", f"{day} 09:10"],
        utc=True,
    )
    return {
        # as_unit("ns") forces true nanosecond ints regardless of pandas's
        # default DatetimeIndex resolution — Shioaji's raw ts is always ns.
        "ts": taipei_wall_clock.as_unit("ns").astype("int64").tolist(),
        "open": [20100.0, 20120.0, 20150.0],
        "high": [20130.0, 20160.0, 20180.0],
        "low": [20080.0, 20100.0, 20140.0],
        "close": [20120.0, 20150.0, 20170.0],
        "volume": [500, 600, 450],
    }


def _taipei(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="Asia/Taipei").tz_convert("UTC")


def _raw_kbars(
    stamps: pd.DatetimeIndex,
    *,
    through: str | None = None,
    untraded: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Synthetic raw minutes, shaped as Shioaji's kbars() returns them.

    ``stamps`` are the raw stamps: Taipei wall clock, each the minute's end.
    Prices follow a fixed rule so expectations can be worked by hand: the
    i-th minute opens at 10000 + i, reaches 5 points either side, closes 1
    point up and trades one lot, so a bar's volume counts its minutes.

    ``through`` cuts the feed at a raw stamp, as a fetch during the session
    would see it; ``untraded`` removes every raw minute up to it, as a thin
    or newly listed contract would show them; ``extra`` appends one raw
    minute.
    """
    minute = pd.RangeIndex(len(stamps))
    frame = pd.DataFrame(
        {
            "ts": stamps.strftime("%Y-%m-%d %H:%M"),
            "Open": 10000 + minute,
            "High": 10005 + minute,
            "Low": 9995 + minute,
            "Close": 10001 + minute,
            "Volume": 1,
        }
    )
    if untraded is not None:
        frame = frame.loc[frame["ts"] > untraded]
    if through is not None:
        frame = frame.loc[frame["ts"] <= through]
    if extra is not None:
        frame = pd.concat([frame, pd.DataFrame([extra])], ignore_index=True)
    raw = {column: frame[column].tolist() for column in frame.columns}
    wall_clock = pd.DatetimeIndex(pd.to_datetime(frame["ts"])).tz_localize("UTC")
    raw["ts"] = wall_clock.as_unit("ns").asi8.tolist()
    return raw


def _kbars_by_date(raw: dict):
    """A kbars() stand-in answering with the raw minutes stamped on the
    requested Taipei dates, both ends included."""
    stamp_dates = pd.to_datetime(raw["ts"], unit="ns").strftime("%Y-%m-%d")

    def kbars(*, contract, start, end):
        keep = (stamp_dates >= start) & (stamp_dates <= end)
        return {
            column: [v for v, k in zip(values, keep, strict=True) if k]
            for column, values in raw.items()
        }

    return kbars


def _txfr1_kbars(**cuts) -> dict:
    """The TAIFEX trading day 2026-11-02: the night session reads 15:01-05:00
    and the day session 08:46-13:45, so minute 0 starts at 15:00."""
    stamps = pd.date_range("2026-10-30 15:01", "2026-10-31 05:00", freq="min").append(
        pd.date_range("2026-11-02 08:46", "2026-11-02 13:45", freq="min")
    )
    return _raw_kbars(stamps, **cuts)


def _fetch_txfr1(
    timeframe: str,
    kbars: dict | None = None,
    *,
    start: str = "2026-10-30",
    end: str = "2026-11-02",
    **kwargs,
) -> pd.DataFrame:
    adapter = _make_adapter()
    adapter._api.kbars.side_effect = _kbars_by_date(_txfr1_kbars() if kbars is None else kbars)
    adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
    return adapter.fetch_ohlcv(
        "TXFR1",
        timeframe,
        start=start,
        end=end,
        continuous_alias=True,
        **kwargs,
    ).set_index("ts")


def _txfr1_subscription(timeframe: str) -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol="TXFR1",
        timeframe=timeframe,
        calendar_id="XTAIFEX",
        session_mode="extended",
        data_source="shioaji",
        instrument_type="contract_monthly",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchOhlcv:
    def test_returns_correct_columns(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        df = adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)

        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
        assert len(df) == 3
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_returns_utc_minute_starts(self):
        """Shioaji's raw ts is Taipei wall clock as fake-UTC, stamped at the
        minute's end: -8h for the clock, then -1min for the start."""
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        df = adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)

        expected = pd.to_datetime(
            ["2026-04-01 00:59", "2026-04-01 01:04", "2026-04-01 01:09"],
            utc=True,
        )
        assert list(df["ts"]) == list(expected)

    def test_empty_response(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = {}
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        df = adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)

        assert df.empty
        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]

    def test_limit_trims_tail(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        df = adapter.fetch_ohlcv("TXFR1", "1m", limit=2, continuous_alias=True)

        assert len(df) == 2

    def test_drop_incomplete_applies_completed_bar_filter(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        with patch(
            "librae.brokers.shioaji_adapter.drop_incomplete_ohlcv",
            side_effect=lambda df, _timeframe, **_kwargs: df.iloc[:-1],
        ) as drop_incomplete:
            df = adapter.fetch_ohlcv(
                "TXFR1",
                "1m",
                continuous_alias=True,
                drop_incomplete=True,
            )

        assert len(df) == 2
        assert drop_incomplete.call_args.kwargs == {"calendar_id": "XTAIFEX"}

    @pytest.mark.parametrize(
        ("security_type", "expected_calendar"),
        [("FUT", "XTAIFEX"), ("STK", "XTAI")],
    )
    def test_resampling_uses_instrument_calendar(self, security_type, expected_calendar):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        contract = (
            _rolling_contract()
            if security_type == "FUT"
            else SimpleNamespace(security_type="STK", code="2330")
        )
        adapter._resolve_contract = MagicMock(return_value=contract)

        with patch(
            "librae.brokers.shioaji_adapter.resample_session_ohlcv",
            side_effect=lambda frame, _seconds, _calendar: frame,
        ) as resample:
            adapter.fetch_ohlcv(
                "TXFR1",
                "5m",
                # Only a future may be polled without dates.
                **(
                    {"continuous_alias": True}
                    if security_type == "FUT"
                    else {"start": "2026-04-01", "end": "2026-04-01"}
                ),
            )

        assert resample.call_args.args[2] == expected_calendar

    def test_explicit_calendar_override_wins(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract("GDFR1", "GDF202608"))

        with patch(
            "librae.brokers.shioaji_adapter.resample_session_ohlcv",
            side_effect=lambda frame, _seconds, _calendar: frame,
        ) as resample:
            adapter.fetch_ohlcv(
                "GDFR1",
                "5m",
                calendar_id="XTAIFEX_1725",
                continuous_alias=True,
            )

        assert resample.call_args.args[2] == "XTAIFEX_1725"

    def test_long_range_is_chunked_to_shioajis_30_day_kbars_limit(self):
        """https://sinotrade.github.io/zh/tutor/limit/ — kbars() rejects any
        single query spanning more than 30 days; a 74-day request must be
        split into 30/30/14-day windows and concatenated transparently."""
        adapter = _make_adapter()
        adapter._api.kbars.side_effect = [
            _make_kbars_response(day) for day in ("2026-01-02", "2026-02-02", "2026-03-03")
        ]
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        df = adapter.fetch_ohlcv(
            "TXFR1",
            "1m",
            start="2026-01-01",
            end="2026-03-15",
            continuous_alias=True,
        )

        assert adapter._api.kbars.call_count == 3
        assert [call.kwargs["start"] for call in adapter._api.kbars.call_args_list] == [
            "2026-01-01",
            "2026-01-31",
            "2026-03-02",
        ]
        assert [call.kwargs["end"] for call in adapter._api.kbars.call_args_list] == [
            "2026-01-30",
            "2026-03-01",
            "2026-03-15",
        ]
        assert len(df) == 9

    def test_an_end_without_a_start_is_refused(self):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        with pytest.raises(ValueError, match="needs start"):
            adapter.fetch_ohlcv("TXFR1", "1h", end="2026-04-05", continuous_alias=True)
        adapter._api.kbars.assert_not_called()

    def test_a_date_string_with_a_time_is_refused(self):
        """Its date depends on a time zone; an aware datetime says which."""
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            adapter.fetch_ohlcv(
                "TXFR1", "1h", start="2026-11-03T20:00:00+00:00", continuous_alias=True
            )
        adapter._api.kbars.assert_not_called()

    @pytest.mark.parametrize(
        ("start", "end"), [("1999-12-01", "2000-01-05"), ("2026-11-02", "2099-12-31")]
    )
    def test_a_daily_window_past_the_calendar_is_refused(self, start, end):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        with pytest.raises(ValueError, match="session of calendar"):
            adapter.fetch_ohlcv("TXFR1", "1d", start=start, end=end, continuous_alias=True)
        adapter._api.kbars.assert_not_called()

    def test_short_range_issues_a_single_kbars_call(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        adapter.fetch_ohlcv(
            "TXFR1",
            "1m",
            start="2026-04-01",
            end="2026-04-05",
            continuous_alias=True,
        )

        adapter._api.kbars.assert_called_once_with(
            contract=adapter._resolve_contract.return_value,
            start="2026-04-01",
            end="2026-04-05",
        )


class TestTaifexSessionBars:
    """Minutes stamped as Shioaji stamps TXFR1, rebuilt into TAIFEX session
    bars. The night session is minutes 0-839 and the day session 840-1139."""

    def test_minutes_start_at_each_session_open(self):
        minutes = _fetch_txfr1("1m").index

        assert minutes[0] == _taipei("2026-10-30 15:00")
        assert minutes[-1] == _taipei("2026-11-02 13:44")
        assert _taipei("2026-10-31 04:59") in minutes
        assert _taipei("2026-11-02 08:45") in minutes
        assert _taipei("2026-10-31 05:00") not in minutes

    def test_hourly_bars_are_whole_hours_from_each_session_open(self):
        hours = _fetch_txfr1("1h").index

        night = pd.date_range(_taipei("2026-10-30 15:00"), periods=14, freq="h")
        day = pd.date_range(_taipei("2026-11-02 08:45"), periods=5, freq="h")
        assert list(hours) == [*night, *day]

    @pytest.mark.parametrize("timeframe", ["15m", "30m", "1h"])
    def test_no_stub_bar_at_a_session_close(self, timeframe):
        bars = _fetch_txfr1(timeframe).index

        assert _taipei("2026-10-31 05:00") not in bars
        assert _taipei("2026-11-02 13:45") not in bars

    def test_first_day_hour_holds_the_sixty_minutes_from_the_open(self):
        """Raw 08:46-09:45 are minutes 840-899: open 10000 + 840, high
        10005 + 899, low 9995 + 840, close 10001 + 899."""
        bar = _fetch_txfr1("1h").loc[_taipei("2026-11-02 08:45")]

        assert (bar["open"], bar["high"], bar["low"], bar["close"], bar["volume"]) == (
            10840,
            10904,
            10835,
            10900,
            60,
        )

    def test_daily_bar_keeps_the_whole_trading_day(self):
        """Every minute of the trading day, 0-1139."""
        days = _fetch_txfr1("1d")

        assert list(days.index) == [_taipei("2026-10-30 15:00")]
        day = days.iloc[0]
        assert (day["open"], day["high"], day["low"], day["close"], day["volume"]) == (
            10000,
            11144,
            9995,
            11140,
            1140,
        )

    @pytest.mark.parametrize(
        ("closing_stamp", "last_minute", "hour", "four_hours", "four_hour_volume", "day_close"),
        [
            # Day close: kbars() stamps the 13:45:00 trades 13:46.
            (
                "2026-11-02 13:46",
                "2026-11-02 13:44",
                "2026-11-02 12:45",
                "2026-11-02 12:45",
                63,
                20000,
            ),
            # Night close, past midnight into a Saturday: 05:00:00 trades stamped 05:01.
            (
                "2026-10-31 05:01",
                "2026-10-31 04:59",
                "2026-10-31 04:00",
                "2026-10-31 03:00",
                123,
                11140,
            ),
        ],
    )
    def test_trades_at_a_segment_close_join_its_last_bar(
        self, closing_stamp, last_minute, hour, four_hours, four_hour_volume, day_close, caplog
    ):
        """Some contracts' kbars() put the trades matched at a segment close
        in a minute stamped one past it, which starts at the close."""
        closing = {"ts": closing_stamp, "Open": 20000, "High": 20000}
        closing |= {"Low": 20000, "Close": 20000, "Volume": 3}
        kbars = _txfr1_kbars(extra=closing)

        with caplog.at_level("WARNING"):
            minutes = _fetch_txfr1("1m", kbars)
            hours = _fetch_txfr1("1h", kbars)
            four = _fetch_txfr1("4h", kbars)
            days = _fetch_txfr1("1d", kbars)

        assert "dropped" not in caplog.text
        assert len(minutes) == 1140, "no minute of its own"
        last = minutes.loc[_taipei(last_minute)]
        assert (last["high"], last["close"], last["volume"]) == (20000, 20000, 4)
        assert (hours.loc[_taipei(hour), "close"], hours.loc[_taipei(hour), "volume"]) == (
            20000,
            63,
        )
        assert four.loc[_taipei(four_hours), "volume"] == four_hour_volume
        assert (days.iloc[0]["close"], days.iloc[0]["volume"]) == (day_close, 1143)

    def test_a_closing_minute_with_no_minute_before_it_becomes_the_last(self):
        """A thin contract may trade only at the close in the last minute."""
        closing = {"ts": "2026-11-02 13:46", "Open": 20000, "High": 20000}
        closing |= {"Low": 20000, "Close": 20000, "Volume": 3}

        minutes = _fetch_txfr1("1m", _txfr1_kbars(through="2026-11-02 13:40", extra=closing))

        assert minutes.index[-2:].tolist() == [
            _taipei("2026-11-02 13:39"),
            _taipei("2026-11-02 13:44"),
        ]
        assert minutes.iloc[-1]["volume"] == 3

    def test_index_minutes_past_the_xtai_close_are_not_folded(self):
        """Index kbars run minutes past 13:30; none of them is known to hold
        closing trades, so the frame keeps them as they come."""
        from librae.brokers.shioaji_adapter import _kbar_frame

        raw = _raw_kbars(pd.date_range("2026-11-05 13:29", "2026-11-05 13:34", freq="min"))

        frame = _kbar_frame([raw], "XTAI")

        assert frame["ts"].tolist() == [
            _taipei(f"2026-11-05 13:{minute}") for minute in range(28, 34)
        ]
        assert frame["volume"].tolist() == [1] * 6

    def test_a_fold_merges_only_the_closing_minute(self):
        """A repeated minute elsewhere in the answer is left for the caller,
        not summed by the fold."""
        from librae.brokers.shioaji_adapter import _kbar_frame

        repeated = {"ts": "2026-11-02 10:00", "Open": 1, "High": 1, "Low": 1, "Close": 1}
        raw = _txfr1_kbars(extra=repeated | {"Volume": 1})
        closing = {"ts": "2026-11-02 13:46", "Open": 20000, "High": 20000}
        closing |= {"Low": 20000, "Close": 20000, "Volume": 3}
        for column, value in _raw_kbars(pd.DatetimeIndex([]), extra=closing).items():
            raw[column] = raw[column] + value

        frame = _kbar_frame([raw], "XTAIFEX")

        assert (frame["ts"] == _taipei("2026-11-02 09:59")).sum() == 2
        assert frame.loc[frame["ts"] == _taipei("2026-11-02 13:44"), "volume"].tolist() == [4]

    def test_a_print_past_the_closing_minute_is_still_dropped(self, caplog):
        """Only the minute starting at the close holds closing trades; one
        starting later is an off-hours print, dropped as before."""
        late = {"ts": "2026-11-02 13:47", "Open": 20000, "High": 20000}
        late |= {"Low": 20000, "Close": 20000, "Volume": 1}
        kbars = _txfr1_kbars(extra=late)

        with caplog.at_level("WARNING"):
            hours = _fetch_txfr1("1h", kbars)
        days = _fetch_txfr1("1d", kbars)

        assert hours.index[-1] == _taipei("2026-11-02 12:45")
        assert "dropped 1 bar" in caplog.text
        assert days.iloc[0]["close"] == 11140
        assert days.iloc[0]["volume"] == 1140

    @pytest.mark.parametrize(
        ("timeframe", "first_bar"),
        [("15m", "00:00"), ("30m", "00:00"), ("1h", "00:00"), ("4h", "03:00")],
    )
    def test_a_window_from_midnight_drops_the_bucket_it_cuts(self, timeframe, first_bar):
        """The raw 00:00 minute starts at 23:59, the last minute of a bucket
        that opened before the window did."""
        bars = _fetch_txfr1(timeframe, start="2026-10-31")

        assert bars.index[0] == _taipei(f"2026-10-31 {first_bar}")

    @pytest.mark.parametrize(
        ("start", "untraded", "first_bar"),
        [
            ("2026-10-31", "2026-10-31 00:05", "2026-10-31 00:00"),
            ("2026-11-02", "2026-11-02 08:46", "2026-11-02 08:45"),
        ],
    )
    @pytest.mark.parametrize("timeframe", ["15m", "1h"])
    def test_a_quiet_window_start_keeps_the_whole_first_bucket(
        self, start, untraded, first_bar, timeframe
    ):
        """A thin contract that does not trade in a bucket's first minutes
        still has that bucket inside the window."""
        bars = _fetch_txfr1(timeframe, _txfr1_kbars(untraded=untraded), start=start)

        assert bars.index[0] == _taipei(first_bar)

    def test_a_daily_window_reaches_back_to_the_night_before(self):
        """A window from Saturday still returns Monday's whole bar, night
        session included: minutes 0-1139."""
        days = _fetch_txfr1("1d", start="2026-10-31")

        assert list(days.index) == [_taipei("2026-10-30 15:00")]
        assert days.iloc[0]["open"] == 10000
        assert days.iloc[0]["volume"] == 1140


# TAIFEX trading days around a weekend and the 2026-10-23 typhoon closure, as
# (date the night session opens, day-session date). 2026-10-26 was a holiday,
# so the night of 10-22 belongs to 10-27.
_TRADING_DAYS = (
    ("2026-10-21", "2026-10-22"),
    ("2026-10-22", "2026-10-27"),
    ("2026-10-27", "2026-10-28"),
    ("2026-10-28", "2026-10-29"),
    ("2026-10-29", "2026-10-30"),
    ("2026-10-30", "2026-11-02"),
    ("2026-11-02", "2026-11-03"),
    ("2026-11-03", "2026-11-04"),
)


def _trading_day_stamps() -> pd.DatetimeIndex:
    """Raw stamps of every minute of ``_TRADING_DAYS``: 840 night minutes
    stamped from 15:01 and 300 day minutes stamped from 08:46 each."""
    stamps = pd.DatetimeIndex([])
    for night, day in _TRADING_DAYS:
        stamps = stamps.append(pd.date_range(f"{night} 15:01", periods=840, freq="min"))
        stamps = stamps.append(pd.date_range(f"{day} 08:46", periods=300, freq="min"))
    return stamps


class TestDateWindow:
    """A date window returns whole buckets only: intraday buckets that start
    on its Taipei dates and daily bars labelled with them, each holding every
    minute it would hold in an uncut history."""

    @staticmethod
    def _fetch(
        timeframe: str, start: str, end: str, *, untraded: str | None = None
    ) -> tuple[pd.DataFrame, list[tuple[str, str]]]:
        adapter = _make_adapter()
        raw = _raw_kbars(_trading_day_stamps(), untraded=untraded)
        adapter._api.kbars.side_effect = _kbars_by_date(raw)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        bars = adapter.fetch_ohlcv(
            "TXFR1", timeframe, start=start, end=end, continuous_alias=True
        ).set_index("ts")
        queries = [(c.kwargs["start"], c.kwargs["end"]) for c in adapter._api.kbars.call_args_list]
        return bars, queries

    def test_an_aware_datetime_counts_by_its_taipei_date(self):
        """16:00 and 23:00 UTC on 2026-11-03 are 00:00 and 07:00 on
        2026-11-04 in Taipei, so the window is 2026-11-04 alone."""
        adapter = _make_adapter()
        adapter._api.kbars.side_effect = _kbars_by_date(_raw_kbars(_trading_day_stamps()))
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        bars = adapter.fetch_ohlcv(
            "TXFR1",
            "4h",
            start=datetime(2026, 11, 3, 16, tzinfo=UTC),
            end=datetime(2026, 11, 3, 23, tzinfo=UTC),
            continuous_alias=True,
        ).set_index("ts")

        assert list(bars.index) == [
            _taipei("2026-11-04 03:00"),
            _taipei("2026-11-04 08:45"),
            _taipei("2026-11-04 12:45"),
        ]

    def test_a_one_day_daily_window_returns_that_trading_day_whole(self):
        """Without the night before, the bar held 600 of its 1140 minutes;
        the next trading day's night, 540 minutes, is not a bar of this day."""
        days, queries = self._fetch("1d", "2026-11-03", "2026-11-03")

        assert list(days.index) == [_taipei("2026-11-02 15:00")]
        assert days.iloc[0]["volume"] == 1140
        assert queries == [("2026-11-02", "2026-11-03")]

    @pytest.mark.parametrize(("timeframe", "minutes"), [("4h", 240), ("2h", 120)])
    def test_the_last_intraday_bucket_is_filled_past_midnight(self, timeframe, minutes):
        """The 23:00 bucket runs into the next date; cut at midnight it held
        60 minutes."""
        bars, queries = self._fetch(timeframe, "2026-11-03", "2026-11-03")

        assert bars.index[-1] == _taipei("2026-11-03 23:00")
        assert bars.iloc[-1]["volume"] == minutes
        assert queries == [("2026-11-03", "2026-11-04")]

    def test_a_monday_bar_holds_fridays_night(self):
        days, _ = self._fetch("1d", "2026-11-02", "2026-11-02")

        assert list(days.index) == [_taipei("2026-10-30 15:00")]
        assert days.iloc[0]["volume"] == 1140

    def test_a_fridays_last_bucket_is_filled_from_saturday(self):
        bars, _ = self._fetch("4h", "2026-10-30", "2026-10-30")

        assert bars.index[-1] == _taipei("2026-10-30 23:00")
        assert bars.iloc[-1]["volume"] == 240

    def test_the_day_after_a_closure_holds_the_night_before_it(self):
        """No session on 10-23 or 10-26: the night opened on 10-22 is
        traded toward 10-27."""
        days, queries = self._fetch("1d", "2026-10-27", "2026-10-27")

        assert list(days.index) == [_taipei("2026-10-22 15:00")]
        assert days.iloc[0]["volume"] == 1140
        assert queries == [("2026-10-22", "2026-10-27")]

    def test_the_night_before_a_closure_fills_its_last_bucket(self):
        bars, _ = self._fetch("4h", "2026-10-22", "2026-10-22")

        assert bars.index[-1] == _taipei("2026-10-22 23:00")
        assert bars.iloc[-1]["volume"] == 240

    @pytest.mark.parametrize(
        ("start", "end"), [("2026-10-31", "2026-11-01"), ("2026-10-23", "2026-10-26")]
    )
    def test_a_daily_window_without_a_trading_day_does_not_query(self, start, end):
        days, queries = self._fetch("1d", start, end)

        assert days.empty
        assert queries == []

    @pytest.mark.parametrize(
        ("timeframe", "first_bar", "minutes"),
        [("4h", "2026-11-02 23:00", 103), ("1d", "2026-11-02 15:00", 103 + 120 + 300)],
    )
    def test_a_first_trade_inside_a_bucket_keeps_that_bucket(self, timeframe, first_bar, minutes):
        """A contract that first trades at 01:17 has its first H4 bucket open
        at 23:00 the date before, and that bucket is whole: nothing traded
        earlier."""
        bars, _ = self._fetch(timeframe, "2026-11-02", "2026-11-03", untraded="2026-11-03 01:17")

        assert bars.index[0] == _taipei(first_bar)
        assert bars.iloc[0]["volume"] == minutes

    @pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "4h", "1d"])
    @pytest.mark.parametrize(
        ("start", "end"),
        [("2026-10-22", "2026-10-27"), ("2026-10-28", "2026-10-30"), ("2026-11-02", "2026-11-03")],
    )
    def test_every_bar_matches_the_uncut_history(self, timeframe, start, end):
        """A caller that trims the result to its own range keeps the bars a
        fetch of the whole history would give it."""
        bars, _ = self._fetch(timeframe, start, end)

        minutes = pd.DataFrame(_raw_kbars(_trading_day_stamps()))
        minutes.columns = minutes.columns.str.lower()
        minutes.index = shioaji_kbar_starts(minutes.pop("ts"))
        seconds = int(pd.Timedelta(timeframe.replace("m", "min")).total_seconds())
        uncut = resample_session_ohlcv(minutes, seconds, "XTAIFEX")
        assert not bars.empty
        pd.testing.assert_frame_equal(
            bars, uncut.loc[bars.index], check_dtype=False, check_index_type=False
        )


class TestDatelessPoll:
    """Live polls without dates. A dateless kbars() call answers yesterday and
    today by stamp date, so on Monday 2026-11-02 it holds the day session
    alone, while that trading day's bar opened on Friday night."""

    NOW = "2026-11-02 13:50"

    @classmethod
    def _poll(cls, monkeypatch, timeframe: str, limit: int) -> tuple[pd.DataFrame, MagicMock]:
        now = _taipei(cls.NOW)
        monkeypatch.setattr(
            pd.Timestamp, "now", classmethod(lambda _cls, tz=None: now.tz_convert(tz))
        )
        dated = _kbars_by_date(_txfr1_kbars())
        today = now.tz_convert("Asia/Taipei").date()

        def kbars(*, contract, start=None, end=None):
            if start is None and end is None:
                start, end = str(today - timedelta(days=1)), str(today)
            return dated(contract=contract, start=start, end=end)

        adapter = _make_adapter()
        adapter._api.kbars.side_effect = kbars
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        with patch("librae.brokers.base.datetime") as clock:
            clock.now.return_value = now.to_pydatetime()
            bars = adapter.fetch_ohlcv(
                "TXFR1", timeframe, limit=limit, continuous_alias=True, drop_incomplete=True
            )
        return bars.set_index("ts"), adapter._api.kbars

    def test_a_daily_bar_is_the_one_a_dated_fetch_returns(self, monkeypatch):
        polled, kbars = self._poll(monkeypatch, "1d", limit=2)

        whole = _fetch_txfr1("1d").loc[[_taipei("2026-10-30 15:00")]]
        pd.testing.assert_frame_equal(polled, whole, check_dtype=False)
        assert kbars.call_count == 1, "one kbars() query per poll"

    def test_a_warmup_sized_daily_poll_stays_one_query(self, monkeypatch):
        polled, kbars = self._poll(monkeypatch, "1d", limit=5000)

        assert list(polled.index) == [_taipei("2026-10-30 15:00")]
        assert kbars.call_count == 1

    def test_an_intraday_poll_asks_for_the_dateless_window_by_date(self, monkeypatch):
        polled, kbars = self._poll(monkeypatch, "1h", limit=2)

        assert list(polled.index) == [_taipei("2026-11-02 11:45"), _taipei("2026-11-02 12:45")]
        assert kbars.call_args.kwargs["start"] == "2026-11-01"
        assert kbars.call_args.kwargs["end"] == "2026-11-02"


class TestDatelessPollRefusal:
    """The tick path is verified for futures only; polling any other contract
    would spend kbars() queries on every poll."""

    @pytest.mark.parametrize("security_type", ["STK", "IND", "OPT"])
    @pytest.mark.parametrize("timeframe", ["1m", "1d"])
    def test_a_poll_without_dates_is_refused_before_any_query(self, security_type, timeframe):
        adapter = _make_adapter()
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(security_type=security_type, code="2330")
        )

        with pytest.raises(ValueError, match="without a date window"):
            adapter.fetch_ohlcv("2330", timeframe, drop_incomplete=True)

        adapter._api.kbars.assert_not_called()
        adapter._api.subscribe.assert_not_called()

    def test_a_dated_fetch_of_a_stock_still_reads_kbars(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(security_type="STK", code="2330")
        )

        bars = adapter.fetch_ohlcv("2330", "1m", start="2026-04-01", end="2026-04-01")

        assert len(bars) == 3
        adapter._api.kbars.assert_called_once()
        assert adapter._api.kbars.call_args.kwargs["start"] == "2026-04-01"


class TestLiveBarClock:
    """Live polls the same adapter, so completion, the bar clock live
    validates and staleness must agree with the start-stamped bars."""

    @staticmethod
    def _fetch_completed_at(now: str, timeframe: str, *, through: str) -> pd.DataFrame:
        with patch("librae.brokers.base.datetime") as clock:
            clock.now.return_value = _taipei(now).to_pydatetime()
            return _fetch_txfr1(timeframe, _txfr1_kbars(through=through), drop_incomplete=True)

    def test_an_hourly_bar_completes_once_a_later_minute_shows_its_last_one_ended(self):
        forming = self._fetch_completed_at("2026-11-02 09:44:30", "1h", through="2026-11-02 09:45")
        unconfirmed = self._fetch_completed_at(
            "2026-11-02 09:45:00", "1h", through="2026-11-02 09:45"
        )
        completed = self._fetch_completed_at(
            "2026-11-02 09:45:01", "1h", through="2026-11-02 09:46"
        )

        assert forming.index[-1] == _taipei("2026-10-31 04:00")
        assert unconfirmed.index[-1] == _taipei("2026-10-31 04:00")
        assert completed.index[-1] == _taipei("2026-11-02 08:45")
        assert completed.iloc[-1]["close"] == 10900

    def test_the_forming_minute_is_dropped(self):
        minutes = self._fetch_completed_at("2026-11-02 09:00:30", "1m", through="2026-11-02 09:01")

        assert minutes.index[-1] == _taipei("2026-11-02 08:59")

    @pytest.mark.parametrize(
        ("timeframe", "now", "through", "newest"),
        [
            ("1m", "2026-11-02 09:34:00.5", "2026-11-02 09:34", "2026-11-02 09:32"),
            ("1m", "2026-11-02 09:34:01", "2026-11-02 09:35", "2026-11-02 09:33"),
            ("5m", "2026-11-02 09:35:00.5", "2026-11-02 09:35", "2026-11-02 09:25"),
            ("5m", "2026-11-02 09:35:01", "2026-11-02 09:36", "2026-11-02 09:30"),
        ],
    )
    def test_a_closed_bar_waits_for_the_next_minute(self, timeframe, now, through, newest):
        """Shioaji serves a minute short of its final trades after its close:
        on 2026-11-05 TXFR1's 11:33 minute read 81 lots half a second after
        it, 91 once the next minute was served. That next minute is what
        shows the bar ended."""
        bars = self._fetch_completed_at(now, timeframe, through=through)

        assert bars.index[-1] == _taipei(newest)

    def test_the_last_bar_of_a_session_waits_only_the_settle(self):
        held = self._fetch_completed_at("2026-11-02 13:45:30", "1h", through="2026-11-02 13:45")
        settled = self._fetch_completed_at("2026-11-02 13:46:00", "1h", through="2026-11-02 13:45")

        assert held.index[-1] == _taipei("2026-11-02 11:45")
        assert settled.index[-1] == _taipei("2026-11-02 12:45")

    @pytest.mark.parametrize(("timeframe", "length"), [("1m", "1min"), ("1h", "1h")])
    def test_every_bar_is_available_one_bar_after_it_starts(self, timeframe, length):
        bars = _fetch_txfr1(timeframe).index
        subscription = _txfr1_subscription(timeframe)

        _, available_at = normalize_bar_times(bars, None, subscription)

        assert list(available_at) == list(bars + pd.Timedelta(length))

    @pytest.mark.parametrize(
        ("timeframe", "last_bar", "expected"),
        [
            ("M1", "2026-11-02 13:44", "2026-11-02 15:01"),
            ("H1", "2026-11-02 12:45", "2026-11-02 16:00"),
            ("H1", "2026-11-02 10:45", "2026-11-02 12:45"),
            ("D1", "2026-10-30 15:00", "2026-11-03 13:45"),
        ],
    )
    def test_the_next_bar_is_due_after_the_session_break(self, timeframe, last_bar, expected):
        due = next_expected_close(
            _taipei(last_bar).to_pydatetime(), timeframe=timeframe, calendar_id="XTAIFEX"
        )

        assert due == _taipei(expected)


class TestReadOnlyGuard:
    def test_place_order_raises_without_ca(self):
        adapter = _make_adapter(ca_activated=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.place_order({"symbol": "TXFR1", "side": "buy", "quantity": 1})

    def test_get_position_raises_without_ca(self):
        adapter = _make_adapter(ca_activated=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.get_position(_position_request("TXFR1"))


class TestPlaceOrder:
    """Exercises place_order()'s body against a mocked shioaji module — the
    previous test suite only covered the read-only guard (above), never the
    actual sj.Action/FuturesPriceType/OrderType construction, which is
    exactly how a real bug shipped: shioaji >=1.4 moved these enums from
    shioaji.order.* to top-level shioaji.*, and nothing caught it until a
    real order attempt against the live API."""

    def _mock_shioaji_module(self):
        mock_sj = MagicMock()
        mock_sj.Action.Buy = "BUY"
        mock_sj.Action.Sell = "SELL"
        mock_sj.FuturesPriceType.LMT = "FUT_LMT"
        mock_sj.FuturesPriceType.MKT = "FUT_MKT"
        mock_sj.StockPriceType.LMT = "STK_LMT"
        mock_sj.StockPriceType.MKT = "STK_MKT"
        mock_sj.OrderType.ROD = "ROD"
        mock_sj.OrderType.IOC = "IOC"
        mock_sj.OrderType.FOK = "FOK"
        mock_sj.FuturesOrder = MagicMock(return_value="mock_order")
        mock_sj.StockOrder = MagicMock(return_value="mock_order")
        return mock_sj

    def test_prepare_order_rounds_lot_and_preserves_aligned_limit_price(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=_rolling_contract(limit_down=19_000, limit_up=21_000)
        )

        prepared = adapter.prepare_order(
            {
                "symbol": "TXFR1",
                "side": "buy",
                "quantity": 2.9,
                "order_type": "limit",
                "time_in_force": "day",
                "position_effect": "open",
                "price": 20_000.0,
                "price_increment": 1.0,
                "continuous_alias": True,
            }
        )

        assert prepared["quantity"] == 2.0
        assert prepared["price"] == 20_000.0

    def test_prepare_order_types_a_below_one_lot_size(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=_rolling_contract(limit_down=19_000, limit_up=21_000)
        )

        with pytest.raises(OrderBelowVenueMinimumError, match="below one lot"):
            adapter.prepare_order(
                {
                    "symbol": "TXFR1",
                    "side": "sell",
                    "quantity": 0.5,
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "position_effect": "reduce",
                    "continuous_alias": True,
                }
            )

    def test_prepare_order_preserves_price_when_fixed_increment_is_unknown(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=_rolling_contract(limit_down=19_000, limit_up=21_000)
        )

        prepared = adapter.prepare_order(
            {
                "symbol": "CUSTOM_FUTURE",
                "side": "buy",
                "quantity": 1.0,
                "order_type": "limit",
                "time_in_force": "day",
                "position_effect": "open",
                "price": 20_000.5,
                "continuous_alias": True,
            }
        )

        assert prepared["price"] == 20_000.5

    def test_prepare_order_rejects_price_outside_known_fixed_grid(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=_rolling_contract(limit_down=19_000, limit_up=21_000)
        )

        with pytest.raises(ValueError, match="not aligned"):
            adapter.prepare_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1.0,
                    "order_type": "limit",
                    "time_in_force": "day",
                    "position_effect": "open",
                    "price": 20_000.5,
                    "price_increment": 1.0,
                    "continuous_alias": True,
                }
            )

    def test_prepare_limit_order_requires_contract_price_boundaries(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        with pytest.raises(ValueError, match="missing price-limit boundaries"):
            adapter.prepare_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1.0,
                    "order_type": "limit",
                    "time_in_force": "day",
                    "position_effect": "open",
                    "price": 20_000.0,
                    "price_increment": 1.0,
                    "continuous_alias": True,
                }
            )

    def test_prepare_order_rejects_stock_contract(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(security_type="STK", code="2330")
        )

        with pytest.raises(ValueError, match="futures order submission only"):
            adapter.prepare_order(
                {
                    "symbol": "2330",
                    "side": "buy",
                    "quantity": 1.0,
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "position_effect": "open",
                }
            )

    def test_futures_limit_order_uses_futures_price_type(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        mock_trade = MagicMock()
        mock_trade.status.id = "order123"
        mock_trade.status.status = "PendingSubmit"
        adapter._api.place_order.return_value = mock_trade

        mock_sj = self._mock_shioaji_module()
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            result = adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "time_in_force": "day",
                    "position_effect": "open",
                    "price": 17000,
                    "continuous_alias": True,
                }
            )

        mock_sj.FuturesOrder.assert_called_once_with(
            price=17000,
            quantity=1,
            action="BUY",
            price_type="FUT_LMT",
            order_type="ROD",
        )
        assert result["id"] == "order123"
        assert result["status"] == "PendingSubmit"
        assert result["symbol"] == "TXFR1"
        assert result["amount"] == 1.0
        assert result["filled"] == 0.0

    def test_stock_market_order_uses_stock_price_type(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(
            return_value=SimpleNamespace(security_type="STK", code="2330")
        )
        mock_trade = MagicMock()
        mock_trade.status.id = "order456"
        mock_trade.status.status = "Filled"
        adapter._api.place_order.return_value = mock_trade

        mock_sj = self._mock_shioaji_module()
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            adapter.place_order(
                {
                    "symbol": "2330",
                    "side": "sell",
                    "quantity": 1000,
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "position_effect": "open",
                }
            )

        mock_sj.StockOrder.assert_called_once_with(
            price=0,
            quantity=1000,
            action="SELL",
            price_type="STK_MKT",
            order_type="IOC",
        )

    def test_client_order_id_uses_deterministic_six_character_custom_field(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        mock_trade = MagicMock()
        mock_trade.status.id = "order123"
        mock_trade.status.status = "PendingSubmit"
        adapter._api.place_order.return_value = mock_trade
        mock_sj = self._mock_shioaji_module()

        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "position_effect": "open",
                    "client_order_id": "strategy-TXFR1-open-20260101T000000",
                    "continuous_alias": True,
                }
            )

        custom_field = mock_sj.FuturesOrder.call_args.kwargs["custom_field"]
        assert custom_field == adapter.broker_client_order_id("strategy-TXFR1-open-20260101T000000")
        assert len(custom_field) == 6

    def test_futures_market_order_uses_ioc(self):
        """TAIFEX rejects market orders with ROD time-in-force (op_code 9938:
        "市價單不允許當日有效委託(ROD)") -- caught live 2026-07-20. Market orders
        must be IOC; only resting limit orders may use ROD."""
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract("TMFR1", "TMF202608"))
        mock_trade = MagicMock()
        mock_trade.status.id = "order789"
        mock_trade.status.status = "Filled"
        adapter._api.place_order.return_value = mock_trade

        mock_sj = self._mock_shioaji_module()
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            adapter.place_order(
                {
                    "symbol": "TMFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "market",
                    "time_in_force": "ioc",
                    "position_effect": "open",
                    "continuous_alias": True,
                }
            )

        mock_sj.FuturesOrder.assert_called_once_with(
            price=0,
            quantity=1,
            action="BUY",
            price_type="FUT_MKT",
            order_type="IOC",
        )

    def test_futures_limit_order_accepts_explicit_fok(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        mock_trade = MagicMock()
        mock_trade.status.id = "order999"
        mock_trade.status.status = "Filled"
        adapter._api.place_order.return_value = mock_trade

        mock_sj = self._mock_shioaji_module()
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "time_in_force": "fok",
                    "position_effect": "open",
                    "price": 17000,
                    "continuous_alias": True,
                }
            )

        mock_sj.FuturesOrder.assert_called_once_with(
            price=17000,
            quantity=1,
            action="BUY",
            price_type="FUT_LMT",
            order_type="FOK",
        )

    def test_place_order_rejects_gtc(self):
        """TAIFEX has no multi-day resting order; 'gtc' has no ROD/IOC/FOK
        equivalent and must fail closed rather than silently downgrading."""
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        mock_sj = self._mock_shioaji_module()
        with (
            patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj),
            pytest.raises(ValueError, match="shioaji does not support"),
        ):
            adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "time_in_force": "gtc",
                    "position_effect": "open",
                    "price": 17000,
                    "continuous_alias": True,
                }
            )

    def test_place_order_rejects_market_order_with_day(self):
        """Confirms the TAIFEX market+ROD rejection (op_code 9938) is enforced
        with time_in_force as an explicit field."""
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())

        mock_sj = self._mock_shioaji_module()
        with (
            patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj),
            pytest.raises(ValueError, match="shioaji does not support"),
        ):
            adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "market",
                    "time_in_force": "day",
                    "position_effect": "open",
                    "continuous_alias": True,
                }
            )


def test_trade_normalization_uses_cumulative_deals():
    from librae.brokers.shioaji_adapter import ShioajiAdapter

    trade = SimpleNamespace(
        order=SimpleNamespace(id="ord-1", quantity=2, custom_field="ABC123", action="Buy"),
        status=SimpleNamespace(
            id="ord-1",
            status="PartFilled",
            order_quantity=2,
            deal_quantity=1,
            deals=[
                SimpleNamespace(
                    price=100.0,
                    quantity=1,
                    datetime=datetime(2025, 1, 1, tzinfo=UTC),
                    commission=0.2,
                )
            ],
        ),
    )

    result = ShioajiAdapter._trade_to_order(trade, symbol="TXFR1")

    assert result["symbol"] == "TXFR1"
    assert result["side"] == "buy"
    assert result["clientOrderId"] == "ABC123"
    assert result["status"] == "PartFilled"
    assert result["filled"] == 1.0
    assert result["average"] == 100.0
    assert result["commission"] == 0.2
    assert result["executed_at"] == datetime(2025, 1, 1, tzinfo=UTC)


def test_trade_lookup_resolves_continuous_symbol_to_contract_code():
    adapter = _make_adapter(ca_activated=True)
    adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
    matching = SimpleNamespace(contract=SimpleNamespace(code="TXF202608"))
    adapter._api.list_trades.return_value = [
        SimpleNamespace(contract=SimpleNamespace(code="TXF202607")),
        matching,
    ]

    result = adapter._trades("TXFR1")

    assert result == [matching]
    adapter._api.update_status.assert_called_once_with()


def test_trade_normalization_falls_back_to_sent_quantity_before_acknowledgement():
    from librae.brokers.shioaji_adapter import ShioajiAdapter

    trade = SimpleNamespace(
        order=SimpleNamespace(id="ord-1", quantity=2, custom_field="ABC123", action="Buy"),
        status=SimpleNamespace(id="ord-1", status="PendingSubmit", order_quantity=0, deals=[]),
    )

    result = ShioajiAdapter._trade_to_order(trade, symbol="TXFR1")

    assert result["amount"] == 2.0
    assert result["filled"] == 0.0


def test_find_order_rejects_ambiguous_compact_client_id():
    adapter = _make_adapter(ca_activated=True)
    client_order_id = "strategy-TXFR1-open-20260101T000000"
    tag = adapter.broker_client_order_id(client_order_id)
    adapter._trades = MagicMock(
        return_value=[
            SimpleNamespace(order=SimpleNamespace(custom_field=tag)),
            SimpleNamespace(order=SimpleNamespace(custom_field=tag)),
        ]
    )

    with pytest.raises(ValueError, match="duplicate Shioaji custom_field digest"):
        adapter.find_order(client_order_id, "TXFR1")


def test_live_executor_verifies_compact_client_id_before_canonicalizing():
    adapter = _make_adapter(ca_activated=True)
    request = OrderRequest(
        client_order_id="strategy-TXFR1-open-20260101T000000",
        symbol="TXF",
        venue_symbol="TXFR1",
        side="buy",
        quantity=2.0,
        order_type="market",
        submitted_at=datetime(2026, 1, 1, tzinfo=UTC),
        security_type="FUT",
        continuous_alias=True,
    )
    tag = adapter.broker_client_order_id(request.client_order_id)
    adapter._trades = MagicMock(
        return_value=[
            SimpleNamespace(
                order=SimpleNamespace(
                    id="ord-1",
                    quantity=2,
                    custom_field=tag,
                    action="Buy",
                ),
                status=SimpleNamespace(
                    id="ord-1",
                    status="Submitted",
                    order_quantity=2,
                    deal_quantity=0,
                    deals=[],
                ),
            )
        ]
    )
    executor = LiveExecutor(CostModel.zero(), simulation=False, order_adapter=adapter)

    report = executor.find_order(request)

    assert report is not None
    assert report.client_order_id == request.client_order_id
    assert report.symbol == request.symbol
    assert report.side == request.side


@pytest.mark.parametrize(
    ("direction_name", "expected_size"),
    [("Buy", 2.0), ("Sell", -2.0)],
)
def test_position_lookup_preserves_broker_direction(direction_name, expected_size):
    sj = _mock_shioaji_actions()
    adapter = _make_adapter(ca_activated=True)
    adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
    adapter._api.list_positions.return_value = [
        SimpleNamespace(
            code="TXF202608",
            direction=getattr(sj.Action, direction_name),
            quantity=2,
            price=22000,
            pnl=100,
        )
    ]

    with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=sj):
        result = adapter.get_position(_position_request("TXFR1"))

    assert result == {
        "symbol": "TXFR1",
        "size": expected_size,
        "avg_price": 22000.0,
        "unrealized_pnl": 100.0,
    }


def test_exact_future_uses_shioaji_native_contract_code():
    sj = _mock_shioaji_actions()
    adapter = _make_adapter(ca_activated=True)
    adapter._resolve_contract = MagicMock(
        return_value=SimpleNamespace(
            security_type="FUT",
            code="TXF202608",
            target_code="",
        )
    )
    adapter._api.contracts.info.return_value = SimpleNamespace(delivery_month="202608")
    adapter._api.list_positions.return_value = [
        SimpleNamespace(
            code="TXF202608",
            direction=sj.Action.Buy,
            quantity=1,
            price=22000,
            pnl=0,
        )
    ]
    request = PositionRequest(
        symbol="TXF_202608",
        venue_symbol="TXF202608",
        currency="TWD",
        multiplier=200.0,
        contract_month="202608",
    )

    with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=sj):
        result = adapter.get_position(request)

    assert result["symbol"] == "TXF_202608"
    adapter._resolve_contract.assert_called_once_with("TXF202608")
    adapter._api.list_positions.assert_called_once_with(account=adapter._api.futopt_account)


def test_future_without_a_selector_is_refused_at_fetch_time():
    adapter = _make_adapter(ca_activated=True)

    with pytest.raises(ValueError) as excinfo:
        adapter._validate_contract_selection(
            _rolling_contract(),
            continuous_alias=False,
            contract_month=None,
        )

    message = str(excinfo.value)
    assert "Shioaji future requires exactly one of" in message
    assert "the venue's native R1/R2 alias" in message
    assert "contract_month='YYYYMM'" in message


def test_exact_future_rejects_shioaji_delivery_month_mismatch():
    adapter = _make_adapter(ca_activated=True)
    contract = SimpleNamespace(
        security_type="FUT",
        code="TXFI6",
        target_code="",
    )
    adapter._api.contracts.info.return_value = SimpleNamespace(delivery_month="202609")

    with pytest.raises(ValueError, match="contract month mismatch"):
        adapter._validate_contract_selection(
            contract,
            continuous_alias=False,
            contract_month="202612",
        )


def test_exact_future_rejects_shioaji_continuous_alias_code():
    adapter = _make_adapter(ca_activated=True)

    with pytest.raises(ValueError, match="cannot use continuous alias"):
        adapter._validate_contract_selection(
            _rolling_contract(),
            continuous_alias=False,
            contract_month="202608",
        )


def test_position_lookup_rejects_multiple_matching_native_positions():
    sj = _mock_shioaji_actions()
    adapter = _make_adapter(ca_activated=True)
    adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
    adapter._api.list_positions.return_value = [
        SimpleNamespace(
            code="TXF202608",
            direction=sj.Action.Buy,
            quantity=1,
            price=22000,
            pnl=0,
        ),
        SimpleNamespace(
            code="TXF202608",
            direction=sj.Action.Sell,
            quantity=1,
            price=22010,
            pnl=0,
        ),
    ]

    with (
        patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=sj),
        pytest.raises(ValueError, match="ambiguous broker positions"),
    ):
        adapter.get_position(_position_request("TXFR1"))


def test_position_lookup_rejects_unknown_direction():
    sj = _mock_shioaji_actions()
    adapter = _make_adapter(ca_activated=True)
    adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
    adapter._api.list_positions.return_value = [
        SimpleNamespace(
            code="TXF202608",
            direction="unknown",
            quantity=1,
            price=22000,
            pnl=0,
        )
    ]

    with (
        patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=sj),
        pytest.raises(ValueError, match="unknown Shioaji position direction"),
    ):
        adapter.get_position(_position_request("TXFR1"))


class TestResolveContract:
    def test_futures_found(self):
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._api.contracts.get.return_value = mock_contract

        result = adapter._resolve_contract("TXFR1")

        assert result is mock_contract
        adapter._api.contracts.get.assert_called_once_with("TXFR1")

    def test_stocks_found(self):
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._api.contracts.get.return_value = mock_contract

        result = adapter._resolve_contract("2330")

        assert result is mock_contract

    def test_unknown_symbol_raises(self):
        adapter = _make_adapter()
        adapter._api.contracts.get.return_value = None

        with pytest.raises(ValueError, match="Unknown symbol"):
            adapter._resolve_contract("INVALID")


class TestGetBalance:
    def test_returns_margin_equity_for_twd(self):
        adapter = _make_adapter(ca_activated=True)
        margin = MagicMock()
        margin.equity = 500_000.0
        margin.available_margin = 300_000.0
        adapter._api.margin.return_value = margin

        result = adapter.get_balance("TWD")

        assert result == {"free": 300_000.0, "used": 200_000.0, "total": 500_000.0}

    def test_non_twd_currency_fails_explicitly(self):
        adapter = _make_adapter(ca_activated=True)

        with pytest.raises(ValueError, match="supports TWD only"):
            adapter.get_balance("USD")
        adapter._api.margin.assert_not_called()

    def test_missing_margin_fields_fail_explicitly(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._api.margin.return_value = SimpleNamespace(equity=500_000.0)

        with pytest.raises(ValueError, match="missing equity or available_margin"):
            adapter.get_balance("TWD")

    def test_requires_ca(self):
        adapter = _make_adapter(ca_activated=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.get_balance("TWD")


class TestLifecycle:
    def test_close_calls_logout(self):
        adapter = _make_adapter()
        adapter.close()
        adapter._api.logout.assert_called_once()

    def test_context_manager(self):
        adapter = _make_adapter()
        with adapter:
            pass
        adapter._api.logout.assert_called_once()


class TestInit:
    """ShioajiAdapter.__init__ itself — the fixtures above all bypass it via
    __new__, so without these login/CA-activation/read_only go untested."""

    def _mock_sj(self, mock_api: MagicMock) -> MagicMock:
        mock_sj = MagicMock()
        mock_sj.Shioaji.return_value = mock_api
        return mock_sj

    def test_missing_credentials_raises(self):
        from librae.brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        with (
            patch("librae.brokers.shioaji_adapter._require_shioaji"),
            pytest.raises(ValueError, match="credentials"),
        ):
            ShioajiAdapter(credentials=ShioajiCredentials(api_key="", secret_key=""))

    def test_login_without_ca_path_is_read_only(self):
        from librae.brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        with patch(
            "librae.brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s", person_id="p"),
            )
        mock_api.login.assert_called_once_with(api_key="k", secret_key="s")
        mock_api.activate_ca.assert_not_called()
        assert adapter._read_only is True

    def _adapter_with_accounts(self, futopt: object, stock: object, *, simulation: bool = True):
        from librae.brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        mock_api.futopt_account = futopt
        mock_api.stock_account = stock
        with patch(
            "librae.brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            return ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s"),
                simulation=simulation,
            )

    def test_execution_identity_uses_login_accounts_and_simulation(self):
        from librae.live.execution_identity import account_fingerprint

        adapter = self._adapter_with_accounts(
            SimpleNamespace(account_id="F123456789"),
            SimpleNamespace(account_id="S987654321"),
        )

        identity = adapter.execution_identity()
        assert identity.environment == "paper"
        assert identity.endpoint == "sinopac"
        assert identity.account_fingerprint == account_fingerprint(
            "shioaji", "F123456789", "S987654321"
        )
        assert "F123456789" not in identity.summary
        assert "S987654321" not in identity.summary

    def test_execution_identity_covers_a_stock_only_login(self):
        """A Sinopac login without futures permission still trades stocks, so
        it must report an identity rather than blocking live startup."""
        from librae.live.execution_identity import account_fingerprint

        adapter = self._adapter_with_accounts(None, SimpleNamespace(account_id="S987654321"))

        identity = adapter.execution_identity()
        assert identity.account_fingerprint == account_fingerprint("shioaji", "S987654321")

    def test_execution_identity_distinguishes_two_stock_accounts(self):
        first = self._adapter_with_accounts(None, SimpleNamespace(account_id="S111"))
        second = self._adapter_with_accounts(None, SimpleNamespace(account_id="S222"))

        assert (
            first.execution_identity().account_fingerprint
            != second.execution_identity().account_fingerprint
        )

    def test_execution_identity_fails_closed_without_any_account(self):
        adapter = self._adapter_with_accounts(None, None)

        with pytest.raises(ValueError, match="authenticated account identity"):
            adapter.execution_identity()

    def test_trading_enabled_activates_ca_once_and_allows_orders(self):
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        mock_api.place_order.return_value.status.id = "order123"
        mock_api.place_order.return_value.status.status = "PendingSubmit"
        with patch(
            "librae.brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(
                credentials=_trading_credentials(),
                trading_enabled=True,
            )
        mock_api.activate_ca.assert_called_once_with(
            ca_path="/path/to/ca", ca_passwd="pw", person_id="p"
        )
        assert adapter._read_only is False

        adapter._resolve_contract = MagicMock(
            return_value=_rolling_contract(limit_down=19_000, limit_up=21_000)
        )
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=MagicMock()):
            result = adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "time_in_force": "day",
                    "position_effect": "open",
                    "price": 20_000,
                    "continuous_alias": True,
                }
            )
        assert result["id"] == "order123"

    def test_data_only_ignores_an_unusable_ca_path(self):
        """A stale certificate belongs to the trading role, so it must not
        break a caller that only wants market data."""
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        mock_api.kbars.return_value = _make_kbars_response()
        with patch(
            "librae.brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(credentials=_trading_credentials(ca_path="/gone/stale.pfx"))

        mock_api.activate_ca.assert_not_called()
        assert adapter._read_only is True

        adapter._resolve_contract = MagicMock(return_value=_rolling_contract())
        assert len(adapter.fetch_ohlcv("TXFR1", "1m", continuous_alias=True)) == 3
        with pytest.raises(NotImplementedError, match="built for market data"):
            adapter.place_order({"symbol": "TXFR1", "side": "buy", "quantity": 1})

    def test_trading_enabled_without_a_ca_path_fails_closed(self):
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        with (
            patch(
                "librae.brokers.shioaji_adapter._require_shioaji",
                return_value=self._mock_sj(mock_api),
            ),
            pytest.raises(ValueError, match="requires a CA certificate"),
        ):
            ShioajiAdapter(credentials=_trading_credentials(ca_path=""), trading_enabled=True)

    def test_refused_ca_activation_is_not_mistaken_for_success(self):
        """activate_ca reports refusal by returning False, not by raising, so a
        discarded return value would leave a tradable-looking adapter that only
        fails at its first order."""
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        mock_api.activate_ca.return_value = False
        with (
            patch(
                "librae.brokers.shioaji_adapter._require_shioaji",
                return_value=self._mock_sj(mock_api),
            ),
            pytest.raises(ValueError, match="rejected the CA certificate"),
        ):
            ShioajiAdapter(credentials=_trading_credentials(), trading_enabled=True)

        mock_api.logout.assert_called_once()

    def test_ca_activation_returning_none_is_treated_as_success(self):
        """Only an explicit False means refusal — a build that returns None
        must not fail a live run that has a working certificate."""
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        mock_api.activate_ca.return_value = None
        with patch(
            "librae.brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(credentials=_trading_credentials(), trading_enabled=True)

        assert adapter._read_only is False
        mock_api.logout.assert_not_called()

    def test_failed_ca_activation_logs_out_before_propagating(self):
        """login() already succeeded, so a raise out of __init__ would strand
        an open Shioaji session with nobody left holding the adapter."""
        from librae.brokers.shioaji_adapter import ShioajiAdapter

        mock_api = MagicMock()
        mock_api.activate_ca.side_effect = RuntimeError("ca file unreadable")
        with (
            patch(
                "librae.brokers.shioaji_adapter._require_shioaji",
                return_value=self._mock_sj(mock_api),
            ),
            pytest.raises(RuntimeError, match="ca file unreadable"),
        ):
            ShioajiAdapter(credentials=_trading_credentials(), trading_enabled=True)

        mock_api.logout.assert_called_once()

    def test_simulation_flag_passed_through(self):
        from librae.brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        mock_sj = self._mock_sj(mock_api)
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s"), simulation=True
            )
        mock_sj.Shioaji.assert_called_once_with(simulation=True)

    def test_credentials_sandbox_overrides_simulation_param(self):
        """ShioajiCredentials.sandbox (SHIOAJI_SANDBOX) takes precedence,
        mirroring CryptoCredentials.sandbox — orthogonal to RunConfig.mode,
        deliberately not derived from it (see librae/live/engine.py)."""
        from librae.brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        mock_sj = self._mock_sj(mock_api)
        with patch("librae.brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s", sandbox=True),
                simulation=False,
            )
        mock_sj.Shioaji.assert_called_once_with(simulation=True)

    def test_sandbox_string_env_value_coerced_to_bool(self):
        from librae.brokers.shioaji_adapter import ShioajiCredentials

        creds = ShioajiCredentials(api_key="k", secret_key="s", sandbox="true")
        assert creds.sandbox is True


class TestShioajiCredentialPairing:
    """Half a key pair means a typo'd env var name, not a read-only run."""

    def test_key_without_secret_raises(self):
        from librae.brokers.shioaji_adapter import ShioajiCredentials

        with pytest.raises(ValueError, match="api_key and secret_key"):
            ShioajiCredentials(api_key="k")

    def test_both_empty_is_read_only_not_an_error(self):
        from librae.brokers.shioaji_adapter import ShioajiCredentials

        assert ShioajiCredentials().api_key == ""

    def test_ca_stays_optional_for_a_data_only_login(self):
        from librae.brokers.shioaji_adapter import ShioajiCredentials

        creds = ShioajiCredentials(api_key="k", secret_key="s")

        assert creds.ca_path == ""
        assert creds.ca_password == ""
