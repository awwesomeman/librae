"""Tests for strategies.module.data.providers.apewisdom — the bare ApeWisdom HTTP call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from strategies.module.data.providers.apewisdom import fetch


def _mock_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_returns_results_on_success():
    payload = {"results": [{"ticker": "MU", "rank": 12, "mentions": 340}]}
    with patch("strategies.module.data.providers.apewisdom.urllib.request.urlopen", return_value=_mock_response(payload)):
        result = fetch("all-stocks")
    assert result == [{"ticker": "MU", "rank": 12, "mentions": 340}]


def test_fetch_returns_empty_list_on_request_failure():
    with patch("strategies.module.data.providers.apewisdom.urllib.request.urlopen", side_effect=Exception("boom")):
        result = fetch("all-stocks")
    assert result == []


def test_fetch_includes_filter_and_page_in_url():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response({"results": []})

    with patch("strategies.module.data.providers.apewisdom.urllib.request.urlopen", side_effect=_fake_urlopen):
        fetch("wallstreetbets", page=2)

    assert captured["url"] == "https://apewisdom.io/api/v1.0/filter/wallstreetbets/page/2"
