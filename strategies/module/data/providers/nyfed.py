"""Thin NY Fed client — GSCPI (Global Supply Chain Pressure Index), no
business logic.

No API key, not on FRED (searched live 2026-07-22, zero hits) — the NY Fed
only publishes this as a direct file download, monthly, ~4th business day
lag. The file is served as ``.xlsx`` but is actually legacy binary ``.xls``
content (confirmed via file magic bytes) — needs the ``xlrd`` engine, not
``openpyxl``; requires the ``research`` extra (``pip install -e '.[research]'``).
"""
from __future__ import annotations

import io
import urllib.request

import pandas as pd

_URL = "https://www.newyorkfed.org/medialibrary/research/interactives/gscpi/downloads/gscpi_data.xlsx"


def fetch() -> pd.DataFrame:
    """Raw GSCPI monthly series — columns ``[date, value]``, `date` as
    Python ``date`` objects, unsorted rows dropped (the sheet has a few
    leading blank rows before the data starts).

    Full history only — NY Fed's endpoint has no date-range parameter,
    caller slices by date.
    """
    req = urllib.request.Request(_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read()

    df = pd.read_excel(io.BytesIO(raw), sheet_name="GSCPI Monthly Data", engine="xlrd")
    df = df.dropna(subset=["Date", "GSCPI"])
    df["Date"] = pd.to_datetime(df["Date"])
    return df.rename(columns={"Date": "date", "GSCPI": "value"})[["date", "value"]].reset_index(drop=True)
