"""Tests for strategies.module.data.providers.defillama — the bare stablecoin
mcap history fetch."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from strategies.module.data.providers.defillama import fetch_defi_tvl, fetch_stablecoin_mcap


def _mock_response(body: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_extracts_pegged_usd_and_sorts_by_date():
    raw = [
        {"date": "1600041600", "totalCirculatingUSD": {"peggedUSD": 200.0}},
        {"date": "1599955200", "totalCirculatingUSD": {"peggedUSD": 100.0}},
    ]
    body = json.dumps(raw).encode()
    with patch("strategies.module.data.providers.defillama.urllib.request.urlopen", return_value=_mock_response(body)):
        result = fetch_stablecoin_mcap()

    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [100.0, 200.0]


def test_fetch_skips_rows_without_pegged_usd():
    raw = [
        {"date": "1599955200", "totalCirculatingUSD": {"peggedEUR": 50.0}},
        {"date": "1600041600", "totalCirculatingUSD": {"peggedUSD": 200.0}},
    ]
    body = json.dumps(raw).encode()
    with patch("strategies.module.data.providers.defillama.urllib.request.urlopen", return_value=_mock_response(body)):
        result = fetch_stablecoin_mcap()

    assert len(result) == 1
    assert result["value"].iloc[0] == 200.0


def test_fetch_defi_tvl_sorts_by_date():
    raw = [{"date": 1600041600, "tvl": 200.0}, {"date": 1599955200, "tvl": 100.0}]
    body = json.dumps(raw).encode()
    with patch("strategies.module.data.providers.defillama.urllib.request.urlopen", return_value=_mock_response(body)):
        result = fetch_defi_tvl()

    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [100.0, 200.0]
