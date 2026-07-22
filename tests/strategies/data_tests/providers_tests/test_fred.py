"""Tests for strategies.module.data.providers.fred — the bare FRED HTTP call."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from strategies.module.data.providers.fred import fetch


def _mock_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_returns_observations_on_success():
    payload = {"observations": [{"date": "2024-01-02", "value": "4.5"}]}
    with patch.dict("os.environ", {"FRED_API_KEY": "abc123"}):
        with patch("strategies.module.data.providers.fred.urllib.request.urlopen", return_value=_mock_response(payload)):
            result = fetch("BAMLH0A0HYM2", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
    assert result == [{"date": "2024-01-02", "value": "4.5"}]


def test_fetch_raises_without_api_key():
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("FRED_API_KEY", None)
        with pytest.raises(RuntimeError, match="FRED_API_KEY not set"):
            fetch("NFCI", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))


def test_fetch_includes_series_id_and_api_key_in_url():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response({"observations": []})

    with patch.dict("os.environ", {"FRED_API_KEY": "abc123"}):
        with patch("strategies.module.data.providers.fred.urllib.request.urlopen", side_effect=_fake_urlopen):
            fetch("MORTGAGE30US", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert "series_id=MORTGAGE30US" in captured["url"]
    assert "api_key=abc123" in captured["url"]
