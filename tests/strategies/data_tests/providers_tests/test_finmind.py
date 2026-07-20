"""Tests for strategies.data.providers.finmind — the bare FinMind HTTP call."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from strategies.data.providers.finmind import fetch


def _mock_response(payload: dict):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(payload).encode()
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_returns_data_rows_on_success():
    with patch("strategies.data.providers.finmind.urllib.request.urlopen", return_value=_mock_response({"status": 200, "data": [{"a": 1}]})):
        result = fetch("SomeDataset", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))
    assert result == [{"a": 1}]


def test_fetch_raises_on_non_200_status():
    with patch("strategies.data.providers.finmind.urllib.request.urlopen", return_value=_mock_response({"status": 400, "msg": "paid tier only"})):
        with pytest.raises(RuntimeError, match="paid tier only"):
            fetch("PaidDataset", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))


def test_fetch_includes_data_id_and_token_in_url():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response({"status": 200, "data": []})

    with patch("strategies.data.providers.finmind.urllib.request.urlopen", side_effect=_fake_urlopen):
        with patch.dict("os.environ", {"FINMIND_TOKEN": "abc123"}):
            fetch("TaiwanFuturesInstitutionalInvestors", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc), data_id="TX")

    assert "data_id=TX" in captured["url"]
    assert "token=abc123" in captured["url"]


def test_fetch_omits_data_id_when_not_given():
    captured = {}

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _mock_response({"status": 200, "data": []})

    with patch("strategies.data.providers.finmind.urllib.request.urlopen", side_effect=_fake_urlopen):
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("FINMIND_TOKEN", None)
            fetch("TaiwanStockTotalInstitutionalInvestors", datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, tzinfo=timezone.utc))

    assert "data_id" not in captured["url"]
