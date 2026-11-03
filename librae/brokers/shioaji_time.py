"""Shioaji-specific timestamp normalization."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

_TAIPEI_UTC_OFFSET_NS = 8 * 60 * 60 * 1_000_000_000
_KBAR_NS = 60 * 1_000_000_000


def shioaji_kbar_starts(raw_ts: npt.ArrayLike) -> pd.DatetimeIndex:
    """Convert raw Shioaji 1-minute kbar ``ts`` values to UTC bar starts.

    Shioaji encodes Taipei wall-clock time as if it were a UTC epoch in
    nanoseconds, and stamps each minute bar at the minute's end. Its
    documentation does not say which end, but its historical-data examples
    start 2330 at 09:01 and TXFR1 at 08:46, a minute after each open
    (https://sinotrade.github.io/zh/tutor/market_data/historical/). TXFR1
    kbars from 2026-10-27 to 2026-11-03 agree: no minute is stamped 08:45 or
    15:00, the last ones are 13:45 and 05:00, and read as minute ends they
    reproduce TradingView's TAIFEX:TXF1! 08:45 hourly bar of 2026-11-02.
    Index contracts such as IX0001 get the same conversion; that they stamp
    the same way is unverified.
    """
    raw = np.asarray(raw_ts, dtype=np.int64)
    return pd.to_datetime(raw - _TAIPEI_UTC_OFFSET_NS - _KBAR_NS, unit="ns", utc=True)
