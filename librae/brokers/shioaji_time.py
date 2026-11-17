"""Shioaji-specific timestamp normalization."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
import pandas as pd

from librae.core.trading_calendar import (
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    segment_closes,
)

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
    reproduce TradingView's TAIFEX:TXF1! 08:45 hourly bar of 2026-11-02
    (O/H/L/C 45000/45265/44700/45057). Index contracts stamp the same way:
    IX0001's 23,672 minute bars from 2026-06-25 to 2026-11-03 run
    09:01-13:34 and never read 09:00.
    """
    raw = np.asarray(raw_ts, dtype=np.int64)
    return pd.to_datetime(raw - _TAIPEI_UTC_OFFSET_NS - _KBAR_NS, unit="ns", utc=True)


# shioaji 1.7's KBars fields (ts, Open, High, Low, Close, Volume, Amount),
# lower-cased: summing is the right merge only for volume and amount.
_FOLD_COLUMNS = frozenset({"ts", "open", "high", "low", "close", "volume", "amount"})


def fold_closing_minutes(minutes: pd.DataFrame, calendar_id: str) -> pd.DataFrame:
    """Merge a TAIFEX minute starting at a segment close into the segment's last minute.

    TAIFEX matches trades at a segment's closing instant, and for some
    contracts kbars() puts them in a minute of their own stamped one past
    the close, which starts at the close and so lies outside every segment.
    Observed for TMF 202611 on 2026-11-05: TAIFEX's tick file has its last
    day-session trade at 13:45:00 (41905, 1 lot), which kbars() returns
    stamped 13:46; night trades at 05:00:00 come stamped 05:01 the same way.
    TXF and MTX kbars hold them in the minute stamped at the close, which is
    also where the tick stream counts a closing print. A minute that starts
    at a close holds only those trades, since nothing trades between
    segments; any other minute outside the segments is left for the
    resampler to handle.

    The calendar's segment closes are the only closes folded: a contract's
    own earlier last trade, such as an expiring contract's final close, lies
    inside the segment and is the caller's to handle.

    Args:
        minutes: Minute bars as ``shioaji_kbar_starts`` stamps them, sorted
            by a UTC-aware ``ts``. Its other columns are among Shioaji's
            KBars fields, lower-cased: ``open``, ``high``, ``low``,
            ``close``, ``volume`` and ``amount``. The merged minute takes
            the first open, highest high, lowest low and last close, and
            sums volume and amount. Any other column, such as a contract
            identifier, raises ``ValueError`` since no rule merges it: add
            it after folding.
        calendar_id: The bars' trading calendar. Only the TAIFEX calendars
            are folded; any other returns ``minutes`` unchanged, since stock
            and index kbars past the close are unverified, and index kbars
            run minutes past it (see ``shioaji_kbar_starts``), so a minute
            there is not known to hold the closing trades.

    Returns:
        The same columns, the closing minutes merged into the minute before
        them, or moved there when that minute has no trades. The merged
        minute is its segment's last, so ``librae.brokers.base``'s
        ``with_settled_available_at`` stamps its ``available_at`` at the
        segment close plus ``SEGMENT_END_SETTLE``, as it would any segment's
        last bar. Stamp ``available_at`` after folding, not before.
    """
    unexpected = [column for column in minutes.columns if column not in _FOLD_COLUMNS]
    if unexpected:
        raise ValueError(
            f"fold_closing_minutes cannot merge columns {unexpected}; "
            f"pass only {sorted(_FOLD_COLUMNS)} and add the rest after folding"
        )
    if calendar_id not in (TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR):
        return minutes
    starts = pd.DatetimeIndex(minutes["ts"])
    previous = starts - pd.Timedelta(minutes=1)
    at_close = segment_closes(previous, "1m", calendar_id) == starts
    at_close &= segment_closes(starts, "1m", calendar_id).isna()
    if not at_close.any():
        return minutes
    closing = minutes.loc[at_close].assign(ts=previous[at_close])
    rest = minutes.loc[~at_close]
    last = rest["ts"].isin(closing["ts"])
    rules = {column: "sum" for column in minutes.columns if column != "ts"}
    rules.update(open="first", high="max", low="min", close="last")
    # Only each closing minute and the last minute it joins are merged.
    merged = (
        pd.concat([rest.loc[last], closing])
        .sort_values("ts", kind="stable")
        .groupby("ts", sort=True)
        .agg(rules)
        .reset_index()
    )
    return pd.concat([rest.loc[~last], merged]).sort_values("ts", kind="stable", ignore_index=True)
