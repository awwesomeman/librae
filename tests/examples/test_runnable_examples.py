"""Subprocess smoke tests for every command documented in examples/README.md."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from examples._synthetic import session_opens
from examples.custom_data_provider import CompositeBarFetcher

ROOT = Path(__file__).resolve().parents[2]


def test_synthetic_exchange_bars_start_on_real_sessions() -> None:
    timestamps = session_opens("XNYS", start="2024-01-01", periods=3)

    assert timestamps.tolist() == [
        pd.Timestamp("2024-01-02T14:30:00Z"),
        pd.Timestamp("2024-01-03T14:30:00Z"),
        pd.Timestamp("2024-01-04T14:30:00Z"),
    ]


@pytest.mark.parametrize(
    "module,arguments,expected_output",
    [
        ("examples.simple_sma.run", ("--mode", "backtest", "--no-db"), "trades="),
        ("examples.target_weights.run", ("--mode", "backtest", "--no-db"), "trades="),
        ("examples.topk_selection.run", ("--mode", "backtest", "--no-db"), "trades="),
        ("examples.minimum_variance.run", ("--mode", "backtest", "--no-db"), "trades="),
        ("examples.multi_leg_spread.run", ("--mode", "backtest", "--no-db"), "trades="),
        ("examples.custom_data_provider", (), "backtest_trades="),
        ("examples.trade_report", (), "wrote trade_report.csv"),
    ],
)
def test_documented_example_command_runs(
    module: str,
    arguments: tuple[str, ...],
    expected_output: str,
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), existing_pythonpath) if value
    )

    result = subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert expected_output in result.stdout
    assert "Traceback" not in result.stderr


def test_composite_fetcher_keeps_factor_availability_separate_from_bar_availability() -> None:
    bar_available_at = pd.Timestamp("2026-01-01T01:05:00Z")

    def price_fetcher(
        _symbol: str,
        _timeframe: str,
        _limit: int,
        *,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame:
        del drop_incomplete
        return pd.DataFrame(
            {
                "ts": pd.to_datetime(["2026-01-01T01:00:00Z"]),
                "available_at": [bar_available_at],
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [1_000.0],
            }
        )

    def factor_fetcher(_symbol: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "available_at": pd.to_datetime(["2026-01-01T00:30:00Z"]),
                "factor_score": [0.75],
            }
        )

    result = CompositeBarFetcher(
        price_fetcher=price_fetcher,
        factor_fetcher=factor_fetcher,
        max_factor_age=pd.Timedelta("2h"),
    )("BTCUSDT", "1h", 1)

    assert result.loc[0, "available_at"] == bar_available_at
    assert result.loc[0, "factor_available_at"] == pd.Timestamp("2026-01-01T00:30:00Z")
    assert result.loc[0, "factor_score"] == 0.75


def test_stale_factor_does_not_replace_valid_bar_availability() -> None:
    bar_available_at = pd.Timestamp("2026-01-01T01:05:00Z")

    def price_fetcher(
        _symbol: str,
        _timeframe: str,
        _limit: int,
        *,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame:
        del drop_incomplete
        return pd.DataFrame(
            {
                "ts": pd.to_datetime(["2026-01-01T01:00:00Z"]),
                "available_at": [bar_available_at],
                "open": [100.0],
                "high": [101.0],
                "low": [99.0],
                "close": [100.0],
                "volume": [1_000.0],
            }
        )

    def stale_factor_fetcher(_symbol: str) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "available_at": pd.to_datetime(["2025-12-31T20:00:00Z"]),
                "factor_score": [0.75],
            }
        )

    result = CompositeBarFetcher(
        price_fetcher=price_fetcher,
        factor_fetcher=stale_factor_fetcher,
        max_factor_age=pd.Timedelta("2h"),
    )("BTCUSDT", "1h", 1)

    assert result.loc[0, "available_at"] == bar_available_at
    assert pd.isna(result.loc[0, "factor_available_at"])
    assert pd.isna(result.loc[0, "factor_score"])
