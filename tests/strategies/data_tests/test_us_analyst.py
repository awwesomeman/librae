"""Tests for strategies.module.data.us_analyst — Finnhub free-tier analyst factors."""
from __future__ import annotations

from unittest.mock import patch

from strategies.module.data.us_analyst import collect_analyst_recommendation, collect_earnings_surprise


def test_collect_analyst_recommendation_writes_weighted_score_per_period():
    rows = [
        {"period": "2026-07-01", "strongBuy": 18, "buy": 33, "hold": 4, "sell": 1, "strongSell": 0},
        {"period": "2026-06-01", "strongBuy": 18, "buy": 33, "hold": 3, "sell": 1, "strongSell": 0},
    ]
    written_calls = []

    def _fake_write(symbol, factor_name, source, value, *, frequency, ts=None, instrument_type="spot"):
        written_calls.append((symbol, factor_name, source, value, frequency, ts))
        return 1

    with patch("strategies.module.data.us_analyst.finnhub_client.fetch", return_value=rows):
        with patch("strategies.module.data.us_analyst.collect_snapshot_factor", side_effect=_fake_write):
            written = collect_analyst_recommendation("MU")

    assert written == 2
    total0 = 18 + 33 + 4 + 1 + 0
    expected_score0 = (18 * 5 + 33 * 4 + 4 * 3 + 1 * 2 + 0 * 1) / total0
    assert written_calls[0][1] == "us_analyst_recommendation_score"
    assert written_calls[0][3] == expected_score0
    assert written_calls[0][4] == "MN1"


def test_collect_analyst_recommendation_skips_zero_total():
    rows = [{"period": "2026-07-01", "strongBuy": 0, "buy": 0, "hold": 0, "sell": 0, "strongSell": 0}]
    with patch("strategies.module.data.us_analyst.finnhub_client.fetch", return_value=rows):
        with patch("strategies.module.data.us_analyst.collect_snapshot_factor") as mock_write:
            written = collect_analyst_recommendation("MU")
    assert written == 0
    mock_write.assert_not_called()


def test_collect_earnings_surprise_computes_percent_and_skips_unreported():
    payload = {"earningsCalendar": [
        {"date": "2026-06-24", "epsEstimate": 21.4019, "epsActual": 25.11},
        {"date": "2026-09-23", "epsEstimate": 20.0, "epsActual": None},  # not reported yet
    ]}
    written_calls = []

    def _fake_write(symbol, factor_name, source, value, *, frequency, ts=None, instrument_type="spot"):
        written_calls.append((factor_name, value, ts))
        return 1

    with patch("strategies.module.data.us_analyst.finnhub_client.fetch", return_value=payload):
        with patch("strategies.module.data.us_analyst.collect_snapshot_factor", side_effect=_fake_write):
            written = collect_earnings_surprise("MU")

    assert written == 1
    expected_pct = (25.11 - 21.4019) / abs(21.4019) * 100
    assert written_calls[0][0] == "us_earnings_surprise_pct"
    assert abs(written_calls[0][1] - expected_pct) < 1e-9
