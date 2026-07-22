"""Tests for strategies.module.data.providers.finnhub — the bare Finnhub HTTP call."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from strategies.module.data.providers.finnhub import fetch


def _mock_response(payload):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_raises_without_api_key():
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("FINNHUB_API_KEY", None)
        with pytest.raises(RuntimeError, match="FINNHUB_API_KEY not set"):
            fetch("stock/recommendation", {"symbol": "MU"})


def test_fetch_returns_json_on_success():
    with patch.dict("os.environ", {"FINNHUB_API_KEY": "abc123"}):
        with patch("strategies.module.data.providers.finnhub.urllib.request.urlopen", return_value=_mock_response([{"symbol": "MU"}])):
            result = fetch("stock/recommendation", {"symbol": "MU"})
    assert result == [{"symbol": "MU"}]


def test_fetch_includes_symbol_and_token_in_url():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response([])

    with patch.dict("os.environ", {"FINNHUB_API_KEY": "abc123"}):
        with patch("strategies.module.data.providers.finnhub.urllib.request.urlopen", side_effect=_fake_urlopen):
            fetch("stock/recommendation", {"symbol": "MU"})

    assert "symbol=MU" in captured["url"]
    assert "token=abc123" in captured["url"]


def test_fetch_raises_runtime_error_on_http_error():
    import urllib.error

    with patch.dict("os.environ", {"FINNHUB_API_KEY": "abc123"}):
        with patch(
            "strategies.module.data.providers.finnhub.urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("url", 403, "Forbidden", {}, None),
        ):
            with pytest.raises(RuntimeError, match="HTTP 403"):
                fetch("stock/congressional-trading", {"symbol": "MU"})
