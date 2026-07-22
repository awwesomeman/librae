"""US-listed single-stock insider trading — SEC EDGAR Form 4 filings.

    us_insider_net_shares — net open-market shares bought minus sold by
    company insiders, summed per filing date. Only transactionCode "P"
    (open-market purchase) and "S" (open-market sale) count — codes like
    "F" (tax withholding), "A" (award/grant), "M" (option exercise) are
    administrative, not a genuine buy/sell sentiment signal, and would
    swamp the real signal if included.

ts = filing date (Form 4 is due within 2 business days of the trade, so
filing date vs. trade date is a small lag, not the multi-week lag fundamentals
have — still using filing date, not transactionDate, for the same
point-in-time reasoning as ``us_fundamentals.py``: a backtest can't see a
trade before SEC EDGAR actually published it).

    df = get_factor("MU", "us_insider_net_shares", start="2024-01-01")
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime

import pandas as pd

from strategies.module.data.factors import FREQUENCY_IRREGULAR, register_factor_fetcher
from strategies.module.data.providers import sec_edgar

_SOURCE = "sec_edgar"
_OPEN_MARKET_CODES = {"P", "S"}


def _net_shares_from_form4(xml_bytes: bytes) -> float:
    """Sum signed shares (acquired positive, disposed negative) across every
    open-market nonDerivativeTransaction in one Form 4 document."""
    root = ET.fromstring(xml_bytes)
    net = 0.0
    for txn in root.findall(".//nonDerivativeTransaction"):
        code_el = txn.find("./transactionCoding/transactionCode")
        if code_el is None or code_el.text not in _OPEN_MARKET_CODES:
            continue
        shares_el = txn.find("./transactionAmounts/transactionShares/value")
        disposition_el = txn.find("./transactionAmounts/transactionAcquiredDisposedCode/value")
        if shares_el is None or disposition_el is None:
            continue
        shares = float(shares_el.text)
        net += shares if disposition_el.text == "A" else -shares
    return net


def _insider_fetcher(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    submissions = sec_edgar.fetch_submissions(symbol)
    cik = submissions["cik"]
    recent = submissions["filings"]["recent"]

    net_by_date: dict[str, float] = defaultdict(float)
    for form, filing_date, accession, primary_doc in zip(
        recent["form"], recent["filingDate"], recent["accessionNumber"], recent["primaryDocument"],
    ):
        if form != "4":
            continue
        filed = pd.Timestamp(filing_date, tz="UTC")
        if not (start_dt <= filed <= end_dt):
            continue
        document = primary_doc.rsplit("/", 1)[-1]  # strip XSLT viewer subdir, e.g. "xslF345X06/"
        xml_bytes = sec_edgar.fetch_filing_document(cik, accession, document)
        net_by_date[filing_date] += _net_shares_from_form4(xml_bytes)

    if not net_by_date:
        return pd.DataFrame(columns=["timestamp", "value"])
    df = pd.DataFrame(
        {"timestamp": pd.Timestamp(d, tz="UTC"), "value": v} for d, v in net_by_date.items()
    )
    return df.sort_values("timestamp").reset_index(drop=True)


register_factor_fetcher("us_insider_net_shares", _insider_fetcher, source=_SOURCE, frequency=FREQUENCY_IRREGULAR, instrument_type="spot")
