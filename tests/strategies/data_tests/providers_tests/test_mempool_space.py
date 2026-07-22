"""Tests for strategies.module.data.providers.mempool_space."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from strategies.module.data.providers.mempool_space import fetch_difficulty_adjustments, fetch_hashrate, fetch_mempool_stats


def _mock_response(body: bytes):
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.__enter__.return_value = mock_resp
    return mock_resp


def test_fetch_hashrate_extracts_avg_hashrate_sorted():
    import json
    raw = {"hashrates": [
        {"timestamp": 1600041600, "avgHashrate": 200.0},
        {"timestamp": 1599955200, "avgHashrate": 100.0},
    ]}
    with patch("strategies.module.data.providers.mempool_space.urllib.request.urlopen", return_value=_mock_response(json.dumps(raw).encode())):
        result = fetch_hashrate()

    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [100.0, 200.0]


def test_fetch_difficulty_adjustments_extracts_difficulty_field():
    import json
    raw = [[1600041600, 700000, 5e13, 0.02], [1598918400, 698000, 4.9e13, -0.01]]
    with patch("strategies.module.data.providers.mempool_space.urllib.request.urlopen", return_value=_mock_response(json.dumps(raw).encode())):
        result = fetch_difficulty_adjustments()

    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [4.9e13, 5e13]


def test_fetch_mempool_stats_extracts_count_sorted():
    import json
    raw = [{"added": 1600041600, "count": 200.0}, {"added": 1599955200, "count": 100.0}]
    with patch("strategies.module.data.providers.mempool_space.urllib.request.urlopen", return_value=_mock_response(json.dumps(raw).encode())):
        result = fetch_mempool_stats()

    assert list(result.columns) == ["date", "value"]
    assert result["value"].tolist() == [100.0, 200.0]
