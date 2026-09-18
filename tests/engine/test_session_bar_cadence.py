"""Resampler output must survive both cadence gates, on every calendar.

The resampler, ``validate_bar_cadence`` and the backtest loader each used to
decide where a bar closed, so a series librae produced could be rejected by
librae. These tests run the whole path end to end; the calendars with more
than one segment per session are the ones that used to fail, and
``XTAIFEX_1725`` is here because it is the one that never did.

Whether the calendar describes the geometry at all is a per-series question
answered by ``bucket_geometry_is_known``, so every matrix here runs in both
session modes. Bar phase is deliberately not judged: only the close is.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from librae.backtest.engine import _resolve_data_timeframe
from librae.core.market_data import validate_bar_cadence
from librae.core.trading_calendar import _session_segments, resample_session_ohlcv
from librae.core.utils import interval_to_timedelta

CALENDARS = ("XTAIFEX", "XTAIFEX_1725", "XTKS", "XHKG", "XSHG", "XNYS", "24/7")
TIMEFRAMES = ("M15", "H1", "H2", "H3", "H4", "H6", "D1")
SYMBOL = "SYM"

# Under session_mode="extended" a regular-session calendar describes none of
# the feed's geometry, so these series fall back to the nominal interval and
# the short gap a truncated bucket leaves still reads as an overlap. #249
# teaches bucket_geometry_is_known that these calendars model their own full
# day; when it lands both sets empty and the assertions below must be updated
# deliberately rather than quietly passing.
REJECTED_UNDER_EXTENDED = frozenset(
    {
        ("XTAIFEX", "H4"),
        ("XTAIFEX", "H6"),
        ("XTKS", "H2"),
        ("XTKS", "H4"),
        ("XTKS", "H6"),
        ("XHKG", "H2"),
        ("XHKG", "H4"),
        ("XHKG", "H6"),
        # XSHG's segments are exactly two hours, so no H2 bucket is ever
        # truncated and the nominal rule is satisfied.
        ("XSHG", "H4"),
        ("XSHG", "H6"),
    }
)
# These clear the overlap check but not the loader: with no truncation to
# explain them, the mode of bar-to-bar gaps reads back as another bar size, so
# the declared timeframe is refused exactly as it is on main.
MISLABELLED_UNDER_EXTENDED = frozenset(
    {("XTKS", "H3"), ("XHKG", "H3"), ("XSHG", "H2"), ("XSHG", "H3")}
)


def _session_minutes(calendar_id: str, sessions: int = 12) -> pd.DatetimeIndex:
    """One-minute bars covering whole trading segments, as a feed would give."""
    if calendar_id == "24/7":
        return pd.date_range("2024-06-03", periods=sessions * 1440, freq="1min", tz="UTC")

    import exchange_calendars as xcals

    exchange_id = "XTAI" if calendar_id.startswith("XTAIFEX") else calendar_id
    labels = xcals.get_calendar(exchange_id).sessions_in_range("2024-06-03", "2024-08-30")
    blocks = [
        pd.date_range(segment_open, segment_close, freq="1min", inclusive="left")
        for label in labels[:sessions]
        for segment_open, segment_close in _session_segments(calendar_id, label.date())
    ]
    return pd.DatetimeIndex(np.concatenate([block.values for block in blocks]), tz="UTC").unique()


def _ohlcv(index: pd.DatetimeIndex) -> pd.DataFrame:
    size = len(index)
    return pd.DataFrame(
        {
            "open": np.full(size, 100.0),
            "high": np.full(size, 101.0),
            "low": np.full(size, 99.0),
            "close": np.full(size, 100.5),
            "volume": np.ones(size),
        },
        index=index,
    )


def _resampled(calendar_id: str, timeframe: str) -> pd.DataFrame:
    target_seconds = int(interval_to_timedelta(timeframe).total_seconds())
    return resample_session_ohlcv(
        _ohlcv(_session_minutes(calendar_id)), target_seconds, calendar_id
    )


def _panel(index: pd.DatetimeIndex) -> pd.DataFrame:
    frame = _ohlcv(index)
    frame.index = pd.MultiIndex.from_arrays(
        [[SYMBOL] * len(index), index], names=["symbol", "datetime"]
    )
    return frame


def _load(index: pd.DatetimeIndex, timeframe: str, calendar_id: str, session_mode: str) -> str:
    return _resolve_data_timeframe(
        _panel(index),
        timeframe,
        {SYMBOL: calendar_id},
        session_mode=session_mode,
    )


@pytest.mark.parametrize("calendar_id", CALENDARS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_resampled_bars_pass_both_cadence_gates_under_regular_sessions(
    calendar_id: str, timeframe: str
) -> None:
    resampled = _resampled(calendar_id, timeframe)

    validate_bar_cadence(resampled.index, timeframe, calendar_id, session_mode="regular")

    assert _load(resampled.index, timeframe, calendar_id, "regular") == timeframe


@pytest.mark.parametrize("calendar_id", CALENDARS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_extended_sessions_keep_the_nominal_rule_until_249(
    calendar_id: str, timeframe: str
) -> None:
    """Calendar-sized bars are unaffected; intraday ones fall back until #249."""
    resampled = _resampled(calendar_id, timeframe)
    cell = (calendar_id, timeframe)

    if cell in REJECTED_UNDER_EXTENDED:
        with pytest.raises(ValueError, match=f"overlap timeframe={timeframe}"):
            validate_bar_cadence(resampled.index, timeframe, calendar_id, session_mode="extended")
    else:
        validate_bar_cadence(resampled.index, timeframe, calendar_id, session_mode="extended")

    if cell in REJECTED_UNDER_EXTENDED or cell in MISLABELLED_UNDER_EXTENDED:
        with pytest.raises(ValueError):
            _load(resampled.index, timeframe, calendar_id, "extended")
        return
    assert _load(resampled.index, timeframe, calendar_id, "extended") == timeframe


def test_resampled_taifex_hours_run_through_a_real_backtest() -> None:
    """The shape ``ShioajiAdapter.fetch_ohlcv('TXFR1', '1h')`` returns."""
    from librae.backtest.engine import Backtest
    from librae.core.cost_model import CostModel
    from librae.core.run_config import AccountConfig, RunConfig
    from librae.core.strategy import Strategy

    resampled = _resampled("XTAIFEX", "H1")
    config = RunConfig(
        strategy_name="session-cadence",
        mode="backtest",
        symbols=(SYMBOL,),
        timeframe="H1",
        market="tw_futures",
        data_source="test",
        session_mode="regular",
        account=AccountConfig(currency="TWD", initial_cash=1_000_000.0),
        symbol_cost_overrides={SYMBOL: {"multiplier": 1.0}},
        instrument_overrides={
            SYMBOL: {
                "data_adapter": "test",
                "instrument_type": "contract_monthly",
                "continuous_alias": True,
                "currency": "TWD",
                "calendar_id": "XTAIFEX",
            }
        },
    )

    class DoNothing(Strategy):
        def on_bar(self, ctx):
            return []

    result = Backtest(
        _panel(resampled.index),
        DoNothing(),
        config=config,
        cost_model=CostModel.zero(),
    ).run()

    assert result.position_events == []


def test_truncated_final_bucket_is_cadence_not_overlap() -> None:
    """The gap the fix is about: XTAIFEX H4 leaves 2h15m before the next
    segment opens, which the fixed-duration rule reads as an overlap."""
    index = _resampled("XTAIFEX", "H4").index
    gaps = pd.Series(index).diff().dropna()

    assert bool((gaps < pd.Timedelta(hours=4)).any())


def _hourly(first: str, count: int, step_hours: int = 1) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        [pd.Timestamp(first) + pd.Timedelta(hours=step_hours * hour) for hour in range(count)]
    )


OFF_PHASE_FEEDS = {
    # 24/7 H4 anchored anywhere but midnight, and 24/7 H1 on the half hour:
    # the default crypto path, where no venue defines a phase to align to.
    "24/7-H4": ("24/7", "H4", _hourly("2024-06-03T02:00Z", 12, step_hours=4)),
    "24/7-H1": ("24/7", "H1", _hourly("2024-06-03T00:30Z", 12)),
    # Whole UTC hours inside one XNYS session, which opens at 14:30Z.
    "XNYS-H1": ("XNYS", "H1", _hourly("2024-06-03T15:00Z", 5)),
}


@pytest.mark.parametrize("session_mode", ("regular", "extended"))
@pytest.mark.parametrize("feed", sorted(OFF_PHASE_FEEDS))
def test_a_series_on_its_own_phase_is_accepted(feed: str, session_mode: str) -> None:
    """Phase is the series' own: only the close decides an overlap."""
    calendar_id, timeframe, index = OFF_PHASE_FEEDS[feed]

    validate_bar_cadence(index, timeframe, calendar_id, session_mode=session_mode)

    assert _load(index, timeframe, calendar_id, session_mode) == timeframe


@pytest.mark.parametrize("session_mode", ("regular", "extended"))
def test_out_of_session_bar_still_loads(session_mode: str) -> None:
    """The loader accepted bars outside every segment before #248 and still
    does. The write path rejects them through ``_completion_floor``;
    tightening the loader to match is a separate decision, not this issue's.
    """
    index = _resampled("XNYS", "H1").index.insert(0, pd.Timestamp("2024-06-03T00:00Z"))

    validate_bar_cadence(index, "H1", "XNYS", session_mode=session_mode)

    assert _load(index, "H1", "XNYS", session_mode) == "H1"


@pytest.mark.parametrize("session_mode", ("regular", "extended"))
@pytest.mark.parametrize("declared", ("M15", "M30"))
def test_coarse_data_cannot_pass_as_a_finer_declared_timeframe(
    declared: str, session_mode: str
) -> None:
    index = _resampled("XNYS", "H1").index

    with pytest.raises(ValueError, match=f"config.timeframe={declared} does not match"):
        _load(index, declared, "XNYS", session_mode)


@pytest.mark.parametrize("session_mode", ("regular", "extended"))
def test_the_declared_timeframe_the_data_really_is_loads(session_mode: str) -> None:
    index = _resampled("XNYS", "H1").index

    assert _load(index, "H1", "XNYS", session_mode) == "H1"


@pytest.mark.parametrize("session_mode", ("regular", "extended"))
@pytest.mark.parametrize("calendar_id", CALENDARS)
def test_overlapping_bar_is_rejected(calendar_id: str, session_mode: str) -> None:
    """H1 rather than H4 so the extra bar cannot also change what size the
    series looks like: every calendar here fits several H1 buckets in one
    segment, so the modal intra-segment gap stays an hour."""
    index = _resampled(calendar_id, "H1").index
    overlapping = index.insert(1, index[0] + pd.Timedelta(minutes=30)).sort_values()

    with pytest.raises(ValueError, match="overlap timeframe=H1"):
        validate_bar_cadence(overlapping, "H1", calendar_id, session_mode=session_mode)
    with pytest.raises(ValueError, match="overlap timeframe=H1"):
        _load(overlapping, "H1", calendar_id, session_mode)
