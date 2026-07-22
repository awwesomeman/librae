"""US-listed single-stock "chip" data — currently just short interest.

Not wired through get_factor/register_factor_fetcher like every other
factor here — deliberately. yfinance's short-interest fields are a live
snapshot (current settlement + one prior month, never a backfillable
range), but get_factor's coverage-tracked cache assumes the opposite: call
it once with start=2015 and it'd mark the entire 2015-> now range as
"fully covered" after getting back only 2 real points, silently hiding
every future settlement date from ever being re-fetched.

So this collects append-only instead: call collect_short_interest()
periodically (matches FINRA's ~bi-monthly settlement cadence), it writes
whatever's new straight to external_factors, no coverage-range bookkeeping.

Not registered via register_factor_fetcher, so get_factor() would reject
it outright (checks the fetcher registry before ever touching the DB) —
read with load_short_interest() below instead, straight from the DB.
"""
from __future__ import annotations

import pandas as pd

from strategies.module.data.providers import yahoo

_FACTOR_NAME = "us_short_interest"
_SOURCE = "yahoo"


def collect_short_interest(symbol: str) -> int:
    """Fetch the current short-interest snapshot for `symbol` and write any
    new settlement-date reading(s) to external_factors. Returns rows written
    (0, 1, or 2 — current + prior month, whichever aren't already stored)."""
    from db.timescale_writer import write_external_factor, write_factor_registry

    # Not registered via register_factor_fetcher (see module docstring), so
    # this is the only place factor_registry ever learns about this factor
    # — piggybacked on an already-DB-touching call, not at import time.
    write_factor_registry([{"factor_name": _FACTOR_NAME, "source": _SOURCE, "frequency": "W2"}])

    snap = yahoo.fetch_short_interest_snapshot(symbol)
    points = [
        (snap["date_short_interest"], snap["shares_short"]),
        (snap["shares_short_prior_month_date"], snap["shares_short_prior_month"]),
    ]
    rows = [
        {"timestamp": pd.Timestamp(ts, unit="s", tz="UTC"), "value": float(val)}
        for ts, val in points
        if ts is not None and val is not None
    ]
    if not rows:
        return 0
    df = pd.DataFrame(rows).drop_duplicates(subset="timestamp")
    return write_external_factor(df, symbol, _FACTOR_NAME, _SOURCE, instrument_type="spot")


def load_short_interest(symbol: str, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Read back what collect_short_interest() has written so far — direct
    DB read, bypasses get_factor()'s fetcher-registry requirement."""
    from db.timescale_reader import load_external_factor

    return load_external_factor(symbol, _FACTOR_NAME, _SOURCE, started_at=start, ended_at=end)
