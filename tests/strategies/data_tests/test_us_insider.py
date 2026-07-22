"""Tests for strategies.module.data.us_insider — Form 4 open-market net-shares factor."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from strategies.module.data import us_insider  # noqa: F401  (registers factor fetcher)
from strategies.module.data.factors import _FACTOR_FETCHERS

_FORM4_TEMPLATE = """<?xml version="1.0"?>
<ownershipDocument>
    <nonDerivativeTable>
        <nonDerivativeTransaction>
            <transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
            <transactionAmounts>
                <transactionShares><value>{shares}</value></transactionShares>
                <transactionAcquiredDisposedCode><value>{disposition}</value></transactionAcquiredDisposedCode>
            </transactionAmounts>
        </nonDerivativeTransaction>
    </nonDerivativeTable>
</ownershipDocument>"""


def test_us_insider_net_shares_registered():
    assert "us_insider_net_shares" in _FACTOR_FETCHERS
    _fn, source, instrument_type, freq = _FACTOR_FETCHERS["us_insider_net_shares"]
    assert source == "sec_edgar"
    assert instrument_type == "spot"
    assert freq == "IRREGULAR"


def test_net_shares_from_form4_purchase_is_positive():
    xml = _FORM4_TEMPLATE.format(code="P", shares="100.00", disposition="A").encode()
    assert us_insider._net_shares_from_form4(xml) == 100.0


def test_net_shares_from_form4_sale_is_negative():
    xml = _FORM4_TEMPLATE.format(code="S", shares="100.00", disposition="D").encode()
    assert us_insider._net_shares_from_form4(xml) == -100.0


def test_net_shares_from_form4_ignores_administrative_codes():
    xml = _FORM4_TEMPLATE.format(code="F", shares="663.00", disposition="D").encode()
    assert us_insider._net_shares_from_form4(xml) == 0.0


def test_insider_fetcher_sums_same_day_filings_and_filters_by_date():
    submissions = {
        "cik": "723125",
        "filings": {
            "recent": {
                "form": ["4", "4", "10-K", "4"],
                "filingDate": ["2024-01-05", "2024-01-05", "2024-01-05", "2023-01-05"],
                "accessionNumber": ["0001-24-000001", "0001-24-000002", "0001-24-000003", "0001-23-000001"],
                "primaryDocument": ["xslF345X06/primarydocument.xml", "xslF345X06/primarydocument.xml", "10k.htm", "xslF345X06/primarydocument.xml"],
            }
        },
    }
    buy_xml = _FORM4_TEMPLATE.format(code="P", shares="50.00", disposition="A").encode()
    sell_xml = _FORM4_TEMPLATE.format(code="S", shares="20.00", disposition="D").encode()

    with patch("strategies.module.data.us_insider.sec_edgar.fetch_submissions", return_value=submissions):
        with patch("strategies.module.data.us_insider.sec_edgar.fetch_filing_document", side_effect=[buy_xml, sell_xml]):
            result = us_insider._insider_fetcher(
                "MU", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 31, tzinfo=timezone.utc),
            )

    assert len(result) == 1
    assert result["value"].iloc[0] == 30.0  # 50 bought - 20 sold, 2023 filing excluded by date range


def test_insider_fetcher_empty_when_no_form4_in_range():
    submissions = {"cik": "723125", "filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "primaryDocument": []}}}
    with patch("strategies.module.data.us_insider.sec_edgar.fetch_submissions", return_value=submissions):
        result = us_insider._insider_fetcher(
            "MU", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 31, tzinfo=timezone.utc),
        )
    assert result.empty
    assert list(result.columns) == ["timestamp", "value"]
