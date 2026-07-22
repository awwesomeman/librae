"""Thin Yahoo Finance (yfinance) client — no business logic.

Used by ``strategies/module/data/regime.py`` (dxy_trend) — no crypto-exchange or
Shioaji equivalent for a US Dollar Index series, so this is the one place
this repo reaches for yfinance. Requires the ``research`` extra
(``pip install -e '.[research]'``) — yfinance is imported lazily inside
the function so importing this module (or anything that imports it)
doesn't require the extra to be installed.
"""
from __future__ import annotations

import pandas as pd


def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Raw daily OHLC for `ticker` (e.g. ``"DX-Y.NYB"``) over [start, end].

    Returns columns ``[date, Open, High, Low, Close, Volume]`` — yfinance's
    own column names, un-renamed; flattens the MultiIndex columns yfinance
    returns for a single-ticker download.
    """
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return raw.reset_index().rename(columns={"Date": "date"})
