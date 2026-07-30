from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pandas as pd
import pytest
from librae import build_backtest_artifact, build_market_data_artifact
from librae.backtest.schema import (
    AccountPerformance,
    BacktestOutput,
    EquityCurvePoint,
    RunMetadata,
    StrategyMetrics,
)


def _market_frame(*, timezone: str | None = "UTC") -> pd.DataFrame:
    index = pd.date_range("2026-07-29", periods=2, freq="1h", tz=timezone, name="datetime")
    return pd.DataFrame(
        {
            "open": [100.0, 101.0],
            "high": [102.0, 103.0],
            "low": [99.0, 100.0],
            "close": [101.0, 102.0],
            "volume": [10.0, 12.0],
            "factor_score": [0.2, 0.4],
        },
        index=index,
    )


def _backtest_output() -> BacktestOutput:
    now = datetime(2026, 7, 29, 12, tzinfo=UTC)
    return BacktestOutput(
        run_metadata=RunMetadata(
            run_id="demo-20260729t1200-abcdef",
            strategy="demo",
            symbols=("BTCUSDT",),
            timeframe="1h",
            data_source="fixture",
            started_at=now,
            ended_at=now,
            run_at=now,
        ),
        account=AccountPerformance(
            account_id="main",
            currency="USDT",
            initial_cash=10_000.0,
            final_equity=10_100.0,
            net_pnl=100.0,
            equity_curve=(
                EquityCurvePoint(
                    ts=now,
                    equity=10_100.0,
                    period_return=0.01,
                    drawdown=0.0,
                ),
            ),
            metrics=StrategyMetrics(total_return=0.01),
        ),
        order_events=(),
        position_snapshots=(),
        allocation_snapshots=(),
    )


def test_market_data_artifact_preserves_features_and_adds_identity() -> None:
    artifact = build_market_data_artifact(
        _market_frame(),
        symbol="BTCUSDT",
        timeframe="1h",
        data_source="fixture",
        instrument_type="spot",
    )

    table = artifact.tables["market_data"]
    assert artifact.manifest["artifact_kind"] == "market_data"
    assert table["factor_score"].tolist() == [0.2, 0.4]
    assert set(table["symbol"]) == {"BTCUSDT"}
    assert set(table["timeframe"]) == {"1h"}
    assert str(table["ts"].dt.tz) == "UTC"


def test_market_data_artifact_rejects_naive_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_market_data_artifact(
            _market_frame(timezone=None),
            symbol="BTCUSDT",
            timeframe="1h",
            data_source="fixture",
            instrument_type="spot",
        )


def test_market_data_artifact_accepts_backtest_style_multiindex() -> None:
    frame = _market_frame()
    frame["symbol"] = "BTCUSDT"
    frame = frame.set_index("symbol", append=True).reorder_levels(["symbol", "datetime"])

    artifact = build_market_data_artifact(
        frame,
        symbol="BTCUSDT",
        timeframe="1h",
        data_source="fixture",
        instrument_type="spot",
    )

    assert artifact.tables["market_data"].columns[:2].tolist() == ["symbol", "ts"]


def test_market_data_artifact_rejects_mismatched_identity_column() -> None:
    frame = _market_frame()
    frame["symbol"] = "ETHUSDT"

    with pytest.raises(ValueError, match="symbol column"):
        build_market_data_artifact(
            frame,
            symbol="BTCUSDT",
            timeframe="1h",
            data_source="fixture",
            instrument_type="spot",
        )


def test_backtest_artifact_builds_stable_tables_and_json_manifest() -> None:
    artifact = build_backtest_artifact(_backtest_output(), config_hash="config-123")

    assert artifact.manifest["artifact_kind"] == "backtest_output"
    assert artifact.manifest["config_hash"] == "config-123"
    assert artifact.manifest["run_metadata"]["run_id"] == "demo-20260729t1200-abcdef"
    assert set(artifact.tables) == {
        "accounts",
        "equity_curve",
        "order_events",
        "position_snapshots",
        "allocation_snapshots",
        "funding_cash_flows",
    }
    assert set(artifact.tables["accounts"]["run_id"]) == {"demo-20260729t1200-abcdef"}
    assert artifact.tables["accounts"].loc[0, "total_return"] == pytest.approx(0.01)
    assert artifact.tables["equity_curve"].loc[0, "account_id"] == "main"
    assert artifact.tables["order_events"].empty
    json.dumps(artifact.manifest)


def test_tabular_artifact_can_use_standard_sqlite_writer() -> None:
    artifact = build_backtest_artifact(_backtest_output(), config_hash="config-123")

    with sqlite3.connect(":memory:") as connection:
        for name, table in artifact.tables.items():
            table.to_sql(name, connection, index=False)
        rows = connection.execute("SELECT account_id, total_return FROM accounts").fetchall()

    assert rows == [("main", 0.01)]


def test_backtest_artifact_requires_config_hash() -> None:
    with pytest.raises(ValueError, match="config_hash"):
        build_backtest_artifact(_backtest_output(), config_hash="")
