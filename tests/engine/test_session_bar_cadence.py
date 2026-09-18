"""Resampler output must survive both cadence gates, on every calendar.

The resampler, ``validate_bar_cadence`` and the backtest loader each used to
decide bucket geometry for themselves, so a series librae produced could be
rejected by librae. These tests run the whole path end to end; the calendars
with more than one segment per session are the ones that used to fail, and
``XTAIFEX_1725`` is here because it is the one that never did.
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


def _load(index: pd.DatetimeIndex, timeframe: str, calendar_id: str) -> str:
    return _resolve_data_timeframe(_panel(index), timeframe, {SYMBOL: calendar_id})


@pytest.mark.parametrize("calendar_id", CALENDARS)
@pytest.mark.parametrize("timeframe", TIMEFRAMES)
def test_resampled_bars_pass_both_cadence_gates(calendar_id: str, timeframe: str) -> None:
    resampled = _resampled(calendar_id, timeframe)

    validate_bar_cadence(resampled.index, timeframe, calendar_id)

    assert _load(resampled.index, timeframe, calendar_id) == timeframe


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
    segment opens, which the old fixed-duration rule read as an overlap."""
    index = _resampled("XTAIFEX", "H4").index
    gaps = pd.Series(index).diff().dropna()

    assert bool((gaps < pd.Timedelta(hours=4)).any())


@pytest.mark.parametrize("calendar_id", CALENDARS)
def test_overlapping_bar_is_rejected(calendar_id: str) -> None:
    index = _resampled(calendar_id, "H4").index
    overlapping = index.insert(1, index[0] + pd.Timedelta(hours=1)).sort_values()

    with pytest.raises(ValueError, match="overlap timeframe=H4"):
        validate_bar_cadence(overlapping, "H4", calendar_id)
    with pytest.raises(ValueError, match="overlap timeframe=H4"):
        _load(overlapping, "H4", calendar_id)


@pytest.mark.parametrize("calendar_id", CALENDARS)
def test_bar_starting_off_its_bucket_boundary_is_rejected(calendar_id: str) -> None:
    index = _resampled(calendar_id, "H4").index
    shifted = index.delete(0).insert(0, index[0] + pd.Timedelta(minutes=15))

    with pytest.raises(ValueError, match="not the canonical timeframe=H4"):
        validate_bar_cadence(shifted, "H4", calendar_id)
    with pytest.raises(ValueError, match="not the canonical timeframe=H4"):
        _load(shifted, "H4", calendar_id)
