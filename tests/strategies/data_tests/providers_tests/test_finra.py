"""Tests for strategies.module.data.providers.finra — the bare FINRA HTTP call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from strategies.module.data.providers.finra import fetch


def _mock_response(payload, record_total=None):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.headers = {"record-total": str(record_total if record_total is not None else len(payload))}
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_returns_rows_on_success():
    payload = [{"symbolCode": "MU", "currentShortPositionQuantity": 100}]
    with patch("strategies.module.data.providers.finra.urllib.request.urlopen", return_value=_mock_response(payload)):
        result = fetch("otcMarket", "consolidatedShortInterest", {})
    assert result == payload


def test_fetch_posts_json_body_with_correct_headers_and_url():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = req.headers
        captured["data"] = json.loads(req.data)
        return _mock_response([])

    with patch("strategies.module.data.providers.finra.urllib.request.urlopen", side_effect=_fake_urlopen):
        fetch("otcMarket", "regShoDaily", {"compareFilters": []})

    assert captured["url"] == "https://api.finra.org/data/group/otcMarket/name/regShoDaily"
    assert captured["method"] == "POST"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["data"] == {"compareFilters": [], "limit": 5000, "offset": 0}


def test_fetch_paginates_until_record_total_reached():
    page1 = [{"i": 1}, {"i": 2}]
    page2 = [{"i": 3}]
    responses = [_mock_response(page1, record_total=3), _mock_response(page2, record_total=3)]

    with patch("strategies.module.data.providers.finra.urllib.request.urlopen", side_effect=responses):
        result = fetch("otcMarket", "regShoDaily", {})

    assert result == [{"i": 1}, {"i": 2}, {"i": 3}]


def test_fetch_single_page_does_not_over_request():
    with patch("strategies.module.data.providers.finra.urllib.request.urlopen", return_value=_mock_response([{"i": 1}], record_total=1)) as mock_urlopen:
        fetch("otcMarket", "regShoDaily", {})
    assert mock_urlopen.call_count == 1


def test_fetch_stops_on_empty_page_even_if_total_not_reached():
    """Defensive: an inconsistent record-total shouldn't spin forever."""
    with patch("strategies.module.data.providers.finra.urllib.request.urlopen", return_value=_mock_response([], record_total=100)):
        result = fetch("otcMarket", "regShoDaily", {})
    assert result == []
