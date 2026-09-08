"""Tests for explicit backtest bar normalization."""

from __future__ import annotations

import pandas as pd
import pytest
from librae import MarketDataSubscription, normalize_bars


def _bars(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [10.0, 11.0],
            "signal": [0.1, 0.2],
        },
        index=index,
    )


def _subscription(
    *,
    timeframe: str = "1h",
    calendar_id: str = "24/7",
    session_mode: str = "extended",
) -> MarketDataSubscription:
    return MarketDataSubscription(
        symbol="AAA",
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source="fixture",
        instrument_type="spot",
    )


def test_normalize_bars_accepts_single_symbol_datetime_index() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="Asia/Taipei"))

    result = normalize_bars(data, symbol="2330")

    assert result.index.names == ["symbol", "datetime"]
    assert str(result.index.get_level_values("datetime").tz) == "UTC"
    assert result["signal"].tolist() == [0.1, 0.2]


def test_normalize_bars_maps_long_form_columns() -> None:
    data = pd.DataFrame(
        {
            "ticker": ["AAA", "AAA"],
            "date": pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"),
            "Open": [100.0, 101.0],
            "High": [101.0, 102.0],
            "Low": [99.0, 100.0],
            "Close": [100.5, 101.5],
            "Volume": [10.0, 11.0],
        }
    )

    result = normalize_bars(
        data,
        column_mapping={
            "ticker": "symbol",
            "date": "datetime",
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        },
    )

    assert result.index.names == ["symbol", "datetime"]
    assert result.index.get_level_values("symbol").unique().tolist() == ["AAA"]


def test_normalize_bars_sorts_canonical_data_without_mutating_input() -> None:
    index = pd.MultiIndex.from_arrays(
        [
            ["BBB", "AAA"],
            pd.to_datetime(["2026-01-02", "2026-01-01"], utc=True),
        ],
        names=["symbol", "datetime"],
    )
    data = _bars(index)

    result = normalize_bars(data)

    assert result.index.get_level_values("symbol").tolist() == ["AAA", "BBB"]
    assert data.index.get_level_values("symbol").tolist() == ["BBB", "AAA"]


def test_normalize_bars_rejects_naive_timestamps() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h"))

    with pytest.raises(ValueError, match="timezone-aware"):
        normalize_bars(data, symbol="AAA")


def test_normalize_bars_rejects_ambiguous_symbol_input() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))
    data["symbol"] = "AAA"

    with pytest.raises(ValueError, match="either symbol or a symbol column"):
        normalize_bars(data, symbol="AAA")


def test_normalize_bars_rejects_duplicate_observations() -> None:
    timestamp = pd.Timestamp("2026-01-01", tz="UTC")
    data = _bars(pd.DatetimeIndex([timestamp, timestamp]))

    with pytest.raises(ValueError, match=r"unique \(symbol, datetime\)"):
        normalize_bars(data, symbol="AAA")


def test_subscription_is_canonical_hashable_and_identity_complete() -> None:
    subscription = _subscription(timeframe="1h")

    assert subscription.timeframe == "H1"
    assert {subscription} == {MarketDataSubscription.from_dict(subscription.to_dict())}
    assert set(subscription.to_dict()) == {
        "symbol",
        "timeframe",
        "calendar_id",
        "session_mode",
        "data_source",
        "instrument_type",
    }


def test_subscription_rejects_missing_calendar_and_unknown_instrument_type() -> None:
    with pytest.raises(ValueError, match="calendar_id"):
        _subscription(calendar_id="")
    with pytest.raises(ValueError, match="instrument_type"):
        MarketDataSubscription(
            symbol="AAA",
            timeframe="H1",
            calendar_id="24/7",
            session_mode="extended",
            data_source="fixture",
            instrument_type="future",
        )


def test_normalize_bars_maps_and_preserves_later_provider_availability() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))
    data["published"] = pd.to_datetime(["2026-01-01T01:05:00Z", "2026-01-01T02:07:00Z"])

    result = normalize_bars(
        data,
        symbol="AAA",
        column_mapping={"published": "available_at"},
        subscription=_subscription(),
    )

    assert result["available_at"].tolist() == [
        pd.Timestamp("2026-01-01T01:05:00Z"),
        pd.Timestamp("2026-01-01T02:07:00Z"),
    ]


def test_normalize_bars_derives_24x7_fixed_completion() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))

    result = normalize_bars(data, symbol="AAA", subscription=_subscription())

    assert result["available_at"].tolist() == [
        pd.Timestamp("2026-01-01T01:00:00Z"),
        pd.Timestamp("2026-01-01T02:00:00Z"),
    ]


def test_normalize_bars_rejects_availability_before_completion() -> None:
    data = _bars(pd.date_range("2026-01-01", periods=2, freq="h", tz="UTC"))
    data["available_at"] = pd.to_datetime(["2026-01-01T00:59:00Z", "2026-01-01T02:00:00Z"])

    with pytest.raises(ValueError, match="earlier than bar completion"):
        normalize_bars(data, symbol="AAA", subscription=_subscription())


def test_normalize_bars_uses_exchange_break_as_fixed_bar_completion() -> None:
    data = _bars(
        pd.DatetimeIndex(
            [pd.Timestamp("2026-03-09T03:30:00Z"), pd.Timestamp("2026-03-09T05:00:00Z")]
        )
    )

    result = normalize_bars(
        data,
        symbol="AAA",
        subscription=_subscription(calendar_id="XHKG", session_mode="regular"),
    )

    assert result["available_at"].tolist() == [
        pd.Timestamp("2026-03-09T04:00:00Z"),
        pd.Timestamp("2026-03-09T06:00:00Z"),
    ]


@pytest.mark.parametrize(
    ("timeframe", "timestamps", "expected"),
    [
        (
            "D1",
            ["2026-03-06T14:30:00Z", "2026-03-09T13:30:00Z"],
            ["2026-03-06T21:00:00Z", "2026-03-09T20:00:00Z"],
        ),
        (
            "W1",
            ["2026-11-23T14:30:00Z", "2026-11-30T14:30:00Z"],
            ["2026-11-27T18:00:00Z", "2026-12-04T21:00:00Z"],
        ),
        (
            "MN1",
            ["2026-10-01T13:30:00Z", "2026-11-02T14:30:00Z"],
            ["2026-10-30T20:00:00Z", "2026-11-30T21:00:00Z"],
        ),
    ],
)
def test_normalize_bars_derives_real_calendar_period_close(
    timeframe: str,
    timestamps: list[str],
    expected: list[str],
) -> None:
    data = _bars(pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True)))

    result = normalize_bars(
        data,
        symbol="AAA",
        subscription=_subscription(
            timeframe=timeframe,
            calendar_id="XNYS",
            session_mode="regular",
        ),
    )

    assert result["available_at"].tolist() == list(pd.to_datetime(expected, utc=True))


def test_extended_calendar_bar_requires_provider_availability() -> None:
    data = _bars(
        pd.DatetimeIndex(pd.to_datetime(["2026-03-06T14:30:00Z", "2026-03-09T13:30:00Z"], utc=True))
    )

    with pytest.raises(ValueError, match="available_at is required"):
        normalize_bars(
            data,
            symbol="AAA",
            subscription=_subscription(
                timeframe="D1",
                calendar_id="XNYS",
                session_mode="extended",
            ),
        )


def test_normalize_bars_rejects_noncanonical_calendar_start() -> None:
    data = _bars(
        pd.DatetimeIndex(pd.to_datetime(["2026-03-09T13:31:00Z", "2026-03-10T13:30:00Z"], utc=True))
    )

    with pytest.raises(ValueError, match="not the canonical"):
        normalize_bars(
            data,
            symbol="AAA",
            subscription=_subscription(
                timeframe="D1",
                calendar_id="XNYS",
                session_mode="regular",
            ),
        )
