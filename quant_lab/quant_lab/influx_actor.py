"""
InfluxDBActor — NautilusTrader Actor that writes TradeTicks and PositionChanged
events to InfluxDB and sends Telegram notifications on position changes.

Configuration via environment variables:
    INFLUX_URL       (default: http://localhost:8086)
    INFLUX_ORG
    INFLUX_BUCKET
    INFLUX_TOKEN
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Optional

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
from nautilus_trader.common.actor import Actor, ActorConfig
from nautilus_trader.model.data import TradeTick
from nautilus_trader.model.events import PositionChanged

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    _HTTPX_AVAILABLE = False


# ---------------------------------------------------------------------------
# Pure helpers — testable without Actor infrastructure
# ---------------------------------------------------------------------------

def build_tick_point(tick: TradeTick, bucket: str = "") -> Point:
    """Build an InfluxDB Point from a TradeTick."""
    ts_ns: int = tick.ts_event  # nanoseconds since epoch
    ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
    return (
        Point("trade_ticks")
        .tag("instrument_id", str(tick.instrument_id))
        .field("price", float(tick.price))
        .field("size", float(tick.size))
        .time(ts_dt)
    )


def build_position_point(event: PositionChanged) -> Point:
    """Build an InfluxDB Point from a PositionChanged event."""
    ts_ns: int = event.ts_event
    ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
    pt = (
        Point("position_changes")
        .tag("instrument_id", str(event.instrument_id))
        .tag("side", str(event.side))
        .field("qty", float(event.quantity))
        .time(ts_dt)
    )
    if event.unrealized_pnl is not None:
        pt = pt.field("unrealized_pnl", float(event.unrealized_pnl))
    if event.realized_pnl is not None:
        pt = pt.field("realized_pnl", float(event.realized_pnl))
    return pt


def format_telegram_message(event: PositionChanged) -> str:
    """Format a concise Telegram notification string for a PositionChanged event."""
    side = str(event.side)
    qty = float(event.quantity)
    instrument = str(event.instrument_id)
    parts = [f"[POS] {instrument} {side} qty={qty:.4g}"]
    if event.avg_px_open is not None:
        parts.append(f"avg_px={float(event.avg_px_open):.6g}")
    if event.unrealized_pnl is not None:
        parts.append(f"upnl={float(event.unrealized_pnl):.6g}")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Actor
# ---------------------------------------------------------------------------

class InfluxDBActor(Actor):
    """
    NautilusTrader Actor that:
    - Writes TradeTick data to InfluxDB (measurement: trade_ticks)
    - Writes PositionChanged events to InfluxDB (measurement: position_changes)
    - Sends Telegram notifications on position changes

    All I/O is scheduled as non-blocking asyncio tasks; exceptions are logged
    and never propagate to the trading loop.
    """

    def __init__(self, config: Optional[ActorConfig] = None) -> None:
        super().__init__(config or ActorConfig())

        # InfluxDB config
        self._influx_url: str = os.environ.get("INFLUX_URL", "http://localhost:8086")
        self._influx_org: str = os.environ.get("INFLUX_ORG", "")
        self._influx_bucket: str = os.environ.get("INFLUX_BUCKET", "trading")
        self._influx_token: str = os.environ.get("INFLUX_TOKEN", "")

        # Telegram config
        self._tg_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")

        # Lazily initialised clients (created on first use / on_start)
        self._influx_client: Optional[InfluxDBClientAsync] = None
        self._http_client: Optional["httpx.AsyncClient"] = None  # type: ignore[name-defined]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Initialise async clients when the actor starts."""
        self._influx_client = InfluxDBClientAsync(
            url=self._influx_url,
            token=self._influx_token,
            org=self._influx_org,
        )
        if _HTTPX_AVAILABLE:
            import httpx as _httpx
            self._http_client = _httpx.AsyncClient(timeout=10.0)
        self.log.info("InfluxDBActor started.")

    def on_stop(self) -> None:
        """Schedule graceful shutdown of async clients."""
        asyncio.ensure_future(self._shutdown_clients())

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_trade_tick(self, tick: TradeTick) -> None:
        """Handle a TradeTick — write to InfluxDB asynchronously."""
        asyncio.ensure_future(self._write_tick(tick))

    def on_event(self, event) -> None:  # type: ignore[override]
        """Dispatch PositionChanged to our handler; ignore all others."""
        if isinstance(event, PositionChanged):
            self.on_position_changed(event)

    def on_position_changed(self, event: PositionChanged) -> None:
        """Handle a PositionChanged event — write to InfluxDB and notify Telegram."""
        asyncio.ensure_future(self._write_position(event))
        if self._tg_token and self._tg_chat_id:
            asyncio.ensure_future(self._send_telegram(event))

    # ------------------------------------------------------------------
    # Async workers
    # ------------------------------------------------------------------

    async def _write_tick(self, tick: TradeTick) -> None:
        if self._influx_client is None:
            return
        try:
            point = build_tick_point(tick, self._influx_bucket)
            async with self._influx_client.write_api() as writer:
                await writer.write(bucket=self._influx_bucket, record=point)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"InfluxDB tick write error: {exc}")

    async def _write_position(self, event: PositionChanged) -> None:
        if self._influx_client is None:
            return
        try:
            point = build_position_point(event)
            async with self._influx_client.write_api() as writer:
                await writer.write(bucket=self._influx_bucket, record=point)
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"InfluxDB position write error: {exc}")

    async def _send_telegram(self, event: PositionChanged) -> None:
        if not _HTTPX_AVAILABLE or self._http_client is None:
            return
        try:
            text = format_telegram_message(event)
            url = f"https://api.telegram.org/bot{self._tg_token}/sendMessage"
            await self._http_client.post(url, json={"chat_id": self._tg_chat_id, "text": text})
        except Exception as exc:  # noqa: BLE001
            self.log.error(f"Telegram send error: {exc}")

    async def _shutdown_clients(self) -> None:
        """Close async clients gracefully."""
        if self._influx_client is not None:
            try:
                await self._influx_client.close()
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"InfluxDB client close error: {exc}")
            finally:
                self._influx_client = None

        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as exc:  # noqa: BLE001
                self.log.error(f"httpx client close error: {exc}")
            finally:
                self._http_client = None
