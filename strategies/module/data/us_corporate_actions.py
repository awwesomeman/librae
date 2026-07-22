"""US-listed single-stock corporate actions — dividends, stock splits.

Unlike us_chip.py's short interest, yfinance gives the *full* dated history
here (not a snapshot) — genuinely backfillable, so this goes through the
normal get_factor()/register_factor_fetcher() path like the SEC factors.

ts = ex-date (announced well ahead of time, no lookahead-bias concern the
way filing dates are for fundamentals).

    df = get_factor("MU", "us_dividend", start="2015-01-01")
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

import pandas as pd

from strategies.module.data.factors import FREQUENCY_IRREGULAR, register_factor_fetcher
from strategies.module.data.providers import yahoo

_SOURCE = "yahoo"


def _series_fetcher(fetch_fn: Callable[[str], pd.Series]) -> Callable[[str, datetime, datetime], pd.DataFrame]:
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        series = fetch_fn(symbol)
        if series.empty:
            return pd.DataFrame(columns=["timestamp", "value"])
        df = pd.DataFrame({
            "timestamp": series.index.tz_convert("UTC"),
            "value": series.values,
        })
        return df[(df["timestamp"] >= start_dt) & (df["timestamp"] <= end_dt)].reset_index(drop=True)

    return _fetch


register_factor_fetcher(
    "us_dividend", _series_fetcher(yahoo.fetch_dividends),
    source=_SOURCE, frequency=FREQUENCY_IRREGULAR, instrument_type="spot",
)
register_factor_fetcher(
    "us_split", _series_fetcher(yahoo.fetch_splits),
    source=_SOURCE, frequency=FREQUENCY_IRREGULAR, instrument_type="spot",
)
