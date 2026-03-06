"""Tests for the monitoring skeleton.

Tests pure point-builder helpers without requiring a live InfluxDB instance.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from nautilus_lab.monitoring.metrics import (
    MonitoringConfig,
    build_strategy_point,
    build_system_point,
)
from nautilus_lab.contracts import SCHEMA_VERSION

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# MonitoringConfig defaults
# ---------------------------------------------------------------------------


def test_monitoring_config_defaults(monkeypatch) -> None:
    monkeypatch.delenv("INFLUX_URL", raising=False)
    monkeypatch.delenv("INFLUX_ORG", raising=False)
    monkeypatch.delenv("INFLUX_BUCKET", raising=False)
    monkeypatch.delenv("INFLUX_TOKEN", raising=False)
    cfg = MonitoringConfig()
    assert cfg.url == "http://localhost:8086"
    assert cfg.bucket == "trading"
    assert cfg.org == ""
    assert cfg.schema_version == SCHEMA_VERSION


def test_monitoring_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("INFLUX_URL", "http://influx:8086")
    monkeypatch.setenv("INFLUX_BUCKET", "my_bucket")
    cfg = MonitoringConfig()
    assert cfg.url == "http://influx:8086"
    assert cfg.bucket == "my_bucket"


# ---------------------------------------------------------------------------
# build_strategy_point — pure helper
# ---------------------------------------------------------------------------


def test_build_strategy_point_measurement() -> None:
    pt = build_strategy_point(
        strategy="DemoBreakout_v1",
        symbol="MXFR1",
        timeframe="H1",
        run_id="run001",
        sample="oos",
        benchmark="TWSE",
        total_return=0.05,
        annual_return=0.30,
        sharpe=1.2,
        max_drawdown=-0.03,
        win_rate=0.60,
        trades=10,
        ts=NOW,
    )
    # influxdb_client Point serializes to line protocol
    line = pt.to_line_protocol()
    assert "strategy_performance" in line
    assert "strategy=DemoBreakout_v1" in line
    assert "symbol=MXFR1" in line
    assert "timeframe=H1" in line
    assert "run_id=run001" in line
    assert "sample=oos" in line
    assert "benchmark=TWSE" in line
    assert "schema_version=" in line
    assert "total_return=" in line
    assert "sharpe=" in line
    assert "trades=" in line


def test_build_strategy_point_schema_version_tag() -> None:
    pt = build_strategy_point(
        strategy="s",
        symbol="X",
        timeframe="D1",
        run_id="r1",
        sample="full",
        benchmark="BH",
        total_return=0.0,
        annual_return=0.0,
        sharpe=0.0,
        max_drawdown=0.0,
        win_rate=0.0,
        trades=0,
        schema_version="2.0.0",
        ts=NOW,
    )
    line = pt.to_line_protocol()
    assert "schema_version=2.0.0" in line


# ---------------------------------------------------------------------------
# build_system_point — pure helper
# ---------------------------------------------------------------------------


def test_build_system_heartbeat_measurement() -> None:
    pt = build_system_point(
        host="prod-vm",
        component="influx_actor",
        latency_ms=12.5,
        ts=NOW,
    )
    line = pt.to_line_protocol()
    assert "system_heartbeat" in line
    assert "host=prod-vm" in line
    assert "component=influx_actor" in line
    assert "latency_ms=" in line


def test_build_system_resources_measurement() -> None:
    pt = build_system_point(
        host="prod-vm",
        component="main",
        cpu_pct=45.2,
        mem_pct=60.0,
        ts=NOW,
    )
    line = pt.to_line_protocol()
    assert "system_resources" in line
    assert "cpu_pct=" in line
    assert "mem_pct=" in line
    assert "latency_ms" not in line


def test_build_system_point_schema_version_tag() -> None:
    pt = build_system_point(
        host="h",
        component="c",
        latency_ms=1.0,
        schema_version="1.0.0",
        ts=NOW,
    )
    line = pt.to_line_protocol()
    assert "schema_version=1.0.0" in line


def test_build_system_point_optional_fields_partial() -> None:
    pt = build_system_point(
        host="h",
        component="c",
        cpu_pct=10.0,
        ts=NOW,
    )
    line = pt.to_line_protocol()
    assert "cpu_pct=" in line
    assert "mem_pct" not in line
