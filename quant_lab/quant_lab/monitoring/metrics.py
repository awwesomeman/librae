"""Minimal monitoring skeleton — sends strategy-level and system-level metrics to InfluxDB.

Strategy-level metrics:
  measurement: strategy_signals
  measurement: strategy_performance

System-level metrics:
  measurement: system_heartbeat   (liveness, latency)
  measurement: system_resources   (cpu, memory)

All helpers are pure functions that return influxdb_client.Point objects.
The sender classes wrap async InfluxDB writes and are safe to use from any
asyncio context (no NautilusTrader dependency).

Configuration via MonitoringConfig dataclass or environment variables.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

try:
    from influxdb_client import Point
    from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
    _INFLUX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _INFLUX_AVAILABLE = False
    Point = None  # type: ignore[assignment,misc]
    InfluxDBClientAsync = None  # type: ignore[assignment,misc]

from ..contracts import SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class MonitoringConfig:
    """InfluxDB connection and tagging config for monitoring."""

    url: str = field(default_factory=lambda: os.environ.get("INFLUX_URL", "http://localhost:8086"))
    org: str = field(default_factory=lambda: os.environ.get("INFLUX_ORG", ""))
    bucket: str = field(default_factory=lambda: os.environ.get("INFLUX_BUCKET", "trading"))
    token: str = field(default_factory=lambda: os.environ.get("INFLUX_TOKEN", ""))
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Pure point-builder helpers
# ---------------------------------------------------------------------------


def build_strategy_point(
    *,
    strategy: str,
    symbol: str,
    timeframe: str,
    run_id: str,
    sample: str,
    benchmark: str,
    total_return: float,
    annual_return: float,
    sharpe: float,
    max_drawdown: float,
    win_rate: float,
    trades: int,
    schema_version: str = SCHEMA_VERSION,
    ts: Optional[datetime] = None,
) -> "Point":
    """Build an InfluxDB Point for strategy_performance measurement."""
    if not _INFLUX_AVAILABLE:
        raise ImportError("influxdb-client is not installed")
    ts = ts or datetime.now(tz=timezone.utc)
    return (
        Point("strategy_performance")
        .tag("schema_version", schema_version)
        .tag("strategy", strategy)
        .tag("symbol", symbol)
        .tag("timeframe", timeframe)
        .tag("run_id", run_id)
        .tag("sample", sample)
        .tag("benchmark", benchmark)
        .field("total_return", float(total_return))
        .field("annual_return", float(annual_return))
        .field("sharpe", float(sharpe))
        .field("max_drawdown", float(max_drawdown))
        .field("win_rate", float(win_rate))
        .field("trades", int(trades))
        .time(ts)
    )


def build_system_point(
    *,
    host: str,
    component: str,
    cpu_pct: Optional[float] = None,
    mem_pct: Optional[float] = None,
    latency_ms: Optional[float] = None,
    schema_version: str = SCHEMA_VERSION,
    ts: Optional[datetime] = None,
) -> "Point":
    """Build an InfluxDB Point for system_resources or system_heartbeat measurement.

    If latency_ms is provided → system_heartbeat.
    Otherwise → system_resources.
    """
    if not _INFLUX_AVAILABLE:
        raise ImportError("influxdb-client is not installed")
    ts = ts or datetime.now(tz=timezone.utc)
    measurement = "system_heartbeat" if latency_ms is not None else "system_resources"
    pt = (
        Point(measurement)
        .tag("schema_version", schema_version)
        .tag("host", host)
        .tag("component", component)
        .time(ts)
    )
    if latency_ms is not None:
        pt = pt.field("latency_ms", float(latency_ms))
    if cpu_pct is not None:
        pt = pt.field("cpu_pct", float(cpu_pct))
    if mem_pct is not None:
        pt = pt.field("mem_pct", float(mem_pct))
    return pt


# ---------------------------------------------------------------------------
# Async sender classes
# ---------------------------------------------------------------------------


class StrategyMetricsSender:
    """Sends strategy-level performance metrics to InfluxDB."""

    def __init__(self, config: Optional[MonitoringConfig] = None) -> None:
        self._cfg = config or MonitoringConfig()
        self._client: Optional["InfluxDBClientAsync"] = None

    async def __aenter__(self) -> "StrategyMetricsSender":
        if not _INFLUX_AVAILABLE:
            raise ImportError("influxdb-client is not installed")
        self._client = InfluxDBClientAsync(
            url=self._cfg.url,
            token=self._cfg.token,
            org=self._cfg.org,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def send(
        self,
        *,
        strategy: str,
        symbol: str,
        timeframe: str,
        run_id: str,
        sample: str,
        benchmark: str,
        total_return: float,
        annual_return: float,
        sharpe: float,
        max_drawdown: float,
        win_rate: float,
        trades: int,
        ts: Optional[datetime] = None,
    ) -> None:
        """Write a strategy_performance point."""
        if self._client is None:
            raise RuntimeError("Use as async context manager: async with StrategyMetricsSender() as s:")
        point = build_strategy_point(
            strategy=strategy,
            symbol=symbol,
            timeframe=timeframe,
            run_id=run_id,
            sample=sample,
            benchmark=benchmark,
            total_return=total_return,
            annual_return=annual_return,
            sharpe=sharpe,
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            trades=trades,
            schema_version=self._cfg.schema_version,
            ts=ts,
        )
        async with self._client.write_api() as writer:
            await writer.write(bucket=self._cfg.bucket, record=point)


class SystemMetricsSender:
    """Sends system-level heartbeat and resource metrics to InfluxDB."""

    def __init__(self, config: Optional[MonitoringConfig] = None) -> None:
        self._cfg = config or MonitoringConfig()
        self._client: Optional["InfluxDBClientAsync"] = None

    async def __aenter__(self) -> "SystemMetricsSender":
        if not _INFLUX_AVAILABLE:
            raise ImportError("influxdb-client is not installed")
        self._client = InfluxDBClientAsync(
            url=self._cfg.url,
            token=self._cfg.token,
            org=self._cfg.org,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def send_heartbeat(
        self,
        host: str,
        component: str,
        latency_ms: float,
        ts: Optional[datetime] = None,
    ) -> None:
        """Write a system_heartbeat point."""
        if self._client is None:
            raise RuntimeError("Use as async context manager.")
        point = build_system_point(
            host=host,
            component=component,
            latency_ms=latency_ms,
            schema_version=self._cfg.schema_version,
            ts=ts,
        )
        async with self._client.write_api() as writer:
            await writer.write(bucket=self._cfg.bucket, record=point)

    async def send_resources(
        self,
        host: str,
        component: str,
        cpu_pct: Optional[float] = None,
        mem_pct: Optional[float] = None,
        ts: Optional[datetime] = None,
    ) -> None:
        """Write a system_resources point."""
        if self._client is None:
            raise RuntimeError("Use as async context manager.")
        point = build_system_point(
            host=host,
            component=component,
            cpu_pct=cpu_pct,
            mem_pct=mem_pct,
            schema_version=self._cfg.schema_version,
            ts=ts,
        )
        async with self._client.write_api() as writer:
            await writer.write(bucket=self._cfg.bucket, record=point)
