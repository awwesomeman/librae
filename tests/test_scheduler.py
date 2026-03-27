"""Tests for scripts/monitor/scheduler.py

Covers:
1. Job executes once and TimescaleDB write is called
2. TimescaleDB write failure does not crash the job
3. signal=0 (hold) still writes a Point
4. dry_run skips write

Skills: python, quant
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from monitoring.scheduler import run_job, _env_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(n: int = 200, base: float = 50000.0, seed: int = 42) -> pd.DataFrame:
    """Synthetic OHLCV with column 'ts'."""
    rng = np.random.default_rng(seed)
    ts = pd.date_range("2025-01-01", periods=n, freq="1h", tz="UTC")
    closes = base + np.cumsum(rng.normal(0, 50, n))
    return pd.DataFrame({
        "ts": ts,
        "open": closes - rng.uniform(0, 20, n),
        "high": closes + rng.uniform(0, 30, n),
        "low": closes - rng.uniform(0, 30, n),
        "close": closes,
        "volume": rng.uniform(100, 1000, n),
    })


def _mock_adapter():
    adapter = MagicMock()
    ohlcv_h1 = _make_ohlcv(200)
    ohlcv_d1 = _make_ohlcv(60, seed=99)
    adapter.fetch_ohlcv = MagicMock(side_effect=lambda sym, tf, limit=200: ohlcv_d1 if tf == "1d" else ohlcv_h1)
    return adapter


def _base_cfg(**overrides) -> dict:
    cfg = {
        "symbol": "BTC/USDT",
        "timeframe": "1h",
        "api_key": "",
        "api_secret": "",
    }
    cfg.update(overrides)
    return cfg


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSchedulerJobWritesCalled:
    """1. scheduler job executes once and TimescaleDB write is called."""

    @patch("monitoring.scheduler._write_to_timescale")
    @patch("monitoring.scheduler._build_adapter")
    def test_timescale_write_called_on_success(self, mock_build, mock_write):
        mock_build.return_value = _mock_adapter()
        mock_write.return_value = True

        cfg = _base_cfg()
        result = run_job(cfg=cfg, dry_run=False)

        assert result is not None
        assert "signal" in result
        assert "price" in result
        mock_write.assert_called_once()


class TestSchedulerTimescaleFailureNoCrash:
    """2. TimescaleDB write failure does not crash — job returns summary."""

    @patch("monitoring.scheduler._write_to_timescale")
    @patch("monitoring.scheduler._build_adapter")
    def test_timescale_write_failure_continues(self, mock_build, mock_write):
        mock_build.return_value = _mock_adapter()
        mock_write.return_value = False  # simulate failure

        cfg = _base_cfg()
        result = run_job(cfg=cfg, dry_run=False)

        # Job still returns a result (not None / no exception)
        assert result is not None
        assert "signal" in result
        mock_write.assert_called_once()

    @patch("monitoring.scheduler._write_to_timescale")
    @patch("monitoring.scheduler._build_adapter")
    def test_timescale_write_exception_continues(self, mock_build, mock_write):
        mock_build.return_value = _mock_adapter()
        mock_write.side_effect = Exception("connection refused")

        cfg = _base_cfg()
        # Should NOT raise
        try:
            result = run_job(cfg=cfg, dry_run=False)
        except Exception:
            pytest.fail("run_job should not propagate TimescaleDB exceptions")


class TestSchedulerHoldSignalWritten:
    """3. signal=0 (hold/flat) still produces a Point and writes it."""

    @patch("monitoring.scheduler._write_to_timescale")
    @patch("monitoring.scheduler._build_adapter")
    @patch("monitoring.signal_monitor.compute_exit_conditions")
    @patch("monitoring.signal_monitor.compute_entry_conditions")
    def test_hold_signal_still_writes(self, mock_entry, mock_exit, mock_build, mock_write):
        mock_build.return_value = _mock_adapter()
        mock_write.return_value = True

        # Force no entry/exit → hold signal
        import pandas as pd
        mock_entry.return_value = pd.Series([False] * 500)
        mock_exit.return_value = pd.Series([False] * 500)

        cfg = _base_cfg()
        result = run_job(cfg=cfg, dry_run=False)

        assert result is not None
        # Signal should be 0 (or 0.0 as string)
        sig_val = float(result["signal"])
        assert sig_val == 0.0
        mock_write.assert_called_once()


class TestSchedulerDryRunSkips:
    """4. dry_run skips write."""

    @patch("monitoring.scheduler._write_to_timescale")
    @patch("monitoring.scheduler._build_adapter")
    def test_dry_run_skips_write(self, mock_build, mock_write):
        mock_build.return_value = _mock_adapter()

        cfg = _base_cfg()
        result = run_job(cfg=cfg, dry_run=True)

        assert result is not None
        # _write_to_timescale should NOT be called in dry_run mode
        mock_write.assert_not_called()
