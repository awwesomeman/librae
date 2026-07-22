"""Tests for strategies.module.data.providers.sec_edgar — the bare SEC EDGAR HTTP calls."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from strategies.module.data.providers.sec_edgar import fetch_filing_document, fetch_submissions


def _mock_response(body: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_submissions_rejects_unknown_ticker():
    with pytest.raises(ValueError, match="No CIK mapping"):
        fetch_submissions("NOPE")


def test_fetch_submissions_returns_json():
    payload = {"cik": "723125", "filings": {"recent": {"form": ["4"]}}}
    with patch(
        "strategies.module.data.providers.sec_edgar.urllib.request.urlopen",
        return_value=_mock_response(json.dumps(payload).encode()),
    ):
        result = fetch_submissions("MU")
    assert result == payload


def test_fetch_filing_document_normalizes_cik_and_accession():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response(b"<xml/>")

    with patch("strategies.module.data.providers.sec_edgar.urllib.request.urlopen", side_effect=_fake_urlopen):
        result = fetch_filing_document("0000723125", "0001652149-26-000004", "primarydocument.xml")

    assert captured["url"] == "https://www.sec.gov/Archives/edgar/data/723125/000165214926000004/primarydocument.xml"
    assert result == b"<xml/>"
