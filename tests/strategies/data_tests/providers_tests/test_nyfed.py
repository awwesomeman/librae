"""Tests for strategies.module.data.providers.nyfed — the bare GSCPI file fetch."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from strategies.module.data.providers.nyfed import fetch


def _mock_response(body: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_drops_blank_rows_and_renames_columns():
    # The real file always needs xlrd to parse (legacy binary .xls content
    # despite the .xlsx extension) — patch pd.read_excel directly rather
    # than depending on xlrd/xlwt round-tripping in the test itself.
    raw_sheet = pd.DataFrame({
        "Date": [None, "31-Jan-1998", "28-Feb-1998"],
        "GSCPI": [None, -1.092175, -0.448778],
    })
    with patch("strategies.module.data.providers.nyfed.urllib.request.urlopen", return_value=_mock_response(b"fake")):
        with patch("strategies.module.data.providers.nyfed.pd.read_excel", return_value=raw_sheet) as mock_read:
            result = fetch()

    assert mock_read.call_args.kwargs["sheet_name"] == "GSCPI Monthly Data"
    assert mock_read.call_args.kwargs["engine"] == "xlrd"
    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [-1.092175, -0.448778]
