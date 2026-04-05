"""Tests for librae.core.utils — timeframe utilities and ID generation."""
from __future__ import annotations

import pandas as pd
import pytest

from librae.core.utils import (
    generate_run_id,
    infer_timeframe,
    make_trade_id,
    to_canonical,
    to_ccxt,
)


# ---------------------------------------------------------------------------
# infer_timeframe
# ---------------------------------------------------------------------------


class TestInferTimeframe:
    def _make_index(self, freq: str, periods: int = 100) -> pd.DatetimeIndex:
        return pd.date_range("2024-01-01", freq=freq, periods=periods)

    def test_h1(self) -> None:
        assert infer_timeframe(self._make_index("1h")) == "H1"

    def test_m5(self) -> None:
        assert infer_timeframe(self._make_index("5min")) == "M5"

    def test_m1(self) -> None:
        assert infer_timeframe(self._make_index("1min")) == "M1"

    def test_m15(self) -> None:
        assert infer_timeframe(self._make_index("15min")) == "M15"

    def test_h4(self) -> None:
        assert infer_timeframe(self._make_index("4h")) == "H4"

    def test_d1(self) -> None:
        assert infer_timeframe(self._make_index("1D")) == "D1"

    def test_w1(self) -> None:
        assert infer_timeframe(self._make_index("1W")) == "W1"

    def test_too_few_bars_raises(self) -> None:
        idx = pd.date_range("2024-01-01", freq="1h", periods=3)
        with pytest.raises(ValueError, match="minimum 5"):
            infer_timeframe(idx)

    def test_3h_freq_inferred_as_H3(self) -> None:
        idx = pd.date_range("2024-01-01", freq="3h", periods=10)
        assert infer_timeframe(idx) == "H3"


# ---------------------------------------------------------------------------
# to_ccxt / to_canonical
# ---------------------------------------------------------------------------


class TestTimeframeConversion:
    def test_to_ccxt_from_canonical(self) -> None:
        assert to_ccxt("H1") == "1h"
        assert to_ccxt("M5") == "5m"
        assert to_ccxt("D1") == "1d"

    def test_to_canonical_from_ccxt(self) -> None:
        assert to_canonical("1h") == "H1"
        assert to_canonical("5m") == "M5"
        assert to_canonical("1d") == "D1"

    def test_to_canonical_idempotent(self) -> None:
        assert to_canonical("H1") == "H1"

    def test_to_ccxt_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse timeframe"):
            to_ccxt("xyz")

    def test_to_canonical_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse timeframe"):
            to_canonical("xyz")


# ---------------------------------------------------------------------------
# make_trade_id
# ---------------------------------------------------------------------------


class TestMakeTradeId:
    def test_format(self) -> None:
        assert make_trade_id("run-123", 0) == "run-123-t0000"
        assert make_trade_id("run-123", 42) == "run-123-t0042"


# ---------------------------------------------------------------------------
# generate_run_id
# ---------------------------------------------------------------------------


class TestGenerateRunId:
    def test_format_with_timeframe(self) -> None:
        rid = generate_run_id("MyStrategy", "BTCUSDT", "H1")
        assert rid.startswith("mystrategy-btcusdt-h1-")
        parts = rid.split("-")
        assert len(parts) == 5

    def test_format_no_timeframe(self) -> None:
        rid = generate_run_id("MyStrategy", "BTCUSDT")
        assert rid.startswith("mystrategy-btcusdt-")
        parts = rid.split("-")
        assert len(parts) == 4

    def test_uniqueness(self) -> None:
        ids = {generate_run_id("s", "x", "M5") for _ in range(50)}
        assert len(ids) == 50
