#!/usr/bin/env python3
import asyncio
import os
from datetime import datetime, timezone

from influxdb_client import Point
from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync


async def main() -> None:
    url = os.getenv("INFLUX_URL", "http://localhost:8086")
    org = os.getenv("INFLUX_ORG", "quant_research")
    bucket = os.getenv("INFLUX_BUCKET", "nautilus_signals")
    token = os.getenv("INFLUX_TOKEN") or os.getenv("DOCKER_INFLUXDB_INIT_ADMIN_TOKEN")

    if not token:
        raise RuntimeError("Missing INFLUX_TOKEN (or DOCKER_INFLUXDB_INIT_ADMIN_TOKEN)")

    p = (
        Point("trade_ticks")
        .tag("instrument_id", "MXFR1")
        .field("price", 20123.5)
        .field("size", 1)
        .time(datetime.now(timezone.utc))
    )

    async with InfluxDBClientAsync(url=url, token=token, org=org) as client:
        write_api = client.write_api()
        await write_api.write(bucket=bucket, org=org, record=p)

    print("OK: wrote 1 TradeTick point to InfluxDB")


if __name__ == "__main__":
    asyncio.run(main())
