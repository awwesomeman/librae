"""Tests for DB-first warmup fetcher in LiveTrader."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
from librae.core.run_config import ExecutionPolicy, RunConfig
from tests.conftest import make_test_cfg


def _test_cfg(**overrides) -> RunConfig:
    overrides.setdefault(
        "execution",
        ExecutionPolicy(
            max_bar_volume_participation_rate=None,
            warmup_periods=50,
        ),
    )
    return make_test_cfg(**overrides)


class TestWarmupFetcher:
    """LiveTrader warmup_fetcher uses get_ohlcv for initial load."""

    def test_warmup_from_db_skips_exchange_api(self):
        """When warmup_fetcher returns data, regular fetcher is not called."""
        from librae.live.engine import LiveTrader

        warmup_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=100, freq="1h", tz="UTC"),
                "open": range(1, 101),
                "high": range(1, 101),
                "low": range(1, 101),
                "close": range(1, 101),
                "volume": [100] * 100,
            }
        )
        mock_warmup = MagicMock(return_value=warmup_df)
        mock_fetcher = MagicMock()
        mock_strategy = MagicMock()

        cfg = _test_cfg()
        trader = LiveTrader(
            mock_strategy,
            lambda x: x,
            config=cfg,
            adapter=mock_fetcher,
            warmup_fetcher=mock_warmup,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        assert result is not None
        assert len(result) == 100
        mock_warmup.assert_called_once_with("BTCUSDT", trader._timeframe, 50)
        mock_fetcher.assert_not_called()

    def test_warmup_fetcher_none_uses_regular_fetcher(self):
        """When warmup_fetcher is None, uses regular fetcher for warmup."""
        from librae.live.engine import LiveTrader

        mock_strategy = MagicMock()
        warmup_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=10, freq="1h", tz="UTC"),
                "open": range(1, 11),
                "high": range(1, 11),
                "low": range(1, 11),
                "close": range(1, 11),
                "volume": [100] * 10,
            }
        )
        mock_fetcher = MagicMock(return_value=warmup_df)

        cfg = _test_cfg(
            execution=ExecutionPolicy(
                max_bar_volume_participation_rate=None,
                warmup_periods=10,
            )
        )
        trader = LiveTrader(
            mock_strategy,
            lambda x: x,
            config=cfg,
            adapter=mock_fetcher,
            warmup_fetcher=None,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        result = trader._fetch_with_cache("BTCUSDT")

        mock_fetcher.assert_called_once()
        assert len(result) == 10

    def test_invalid_runtime_ohlcv_is_not_cached(self, caplog):
        from librae.live.engine import LiveTrader

        invalid_df = pd.DataFrame(
            {
                "ts": pd.date_range("2024-01-01", periods=2, freq="1h", tz="UTC"),
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [100.0, float("nan")],
            }
        )
        trader = LiveTrader(
            MagicMock(),
            lambda x: x,
            config=_test_cfg(
                execution=ExecutionPolicy(
                    max_bar_volume_participation_rate=None,
                    warmup_periods=2,
                )
            ),
            adapter=MagicMock(return_value=invalid_df),
            warmup_fetcher=None,
            on_bar=None,
            on_order_event=None,
            on_ohlcv=None,
            on_heartbeat=None,
            on_signal_outcome=None,
        )

        assert trader._fetch_with_cache("BTCUSDT") is None
        assert "BTCUSDT" not in trader._ohlcv_cache
        assert "runtime data OHLCV values must be finite" in caplog.text
