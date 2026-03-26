# InfluxDBActor

NautilusTrader `Actor` that writes live market and position data to InfluxDB
and sends Telegram notifications on position changes.

## Environment variables

| Variable            | Default                   | Description                        |
|---------------------|---------------------------|------------------------------------|
| `INFLUX_URL`        | `http://localhost:8086`   | InfluxDB base URL                  |
| `INFLUX_ORG`        | *(required)*              | InfluxDB organisation name         |
| `INFLUX_BUCKET`     | `trading`                 | InfluxDB bucket name               |
| `INFLUX_TOKEN`      | *(required)*              | InfluxDB API token                 |
| `TELEGRAM_BOT_TOKEN`| *(optional)*              | Telegram bot token — skip if blank |
| `TELEGRAM_CHAT_ID`  | *(optional)*              | Telegram chat/channel ID           |

## InfluxDB measurements

### `trade_ticks`
| Field / Tag   | Kind  | Type   | Notes                            |
|---------------|-------|--------|----------------------------------|
| `instrument_id` | tag | string | e.g. `BTC/USDT.BINANCE`         |
| `price`       | field | float  | Last traded price                |
| `size`        | field | float  | Trade quantity                   |
| timestamp     | time  | ns     | From `TradeTick.ts_event`        |

### `position_changes`
| Field / Tag      | Kind  | Type   | Notes                                    |
|------------------|-------|--------|------------------------------------------|
| `instrument_id`  | tag   | string |                                          |
| `side`           | tag   | string | `LONG` / `SHORT`                         |
| `qty`            | field | float  | Current position quantity                |
| `unrealized_pnl` | field | float  | Present if available, omitted otherwise  |
| `realized_pnl`   | field | float  | Present if available, omitted otherwise  |
| timestamp        | time  | ns     | From `PositionChanged.ts_event`          |

## Sample wiring pseudocode

```python
import asyncio
from nautilus_trader.trading.trader import Trader
from nautilus_trader.common.actor import ActorConfig
from quant_lab.influx_actor import InfluxDBActor

# 1. Create the actor (reads env vars automatically)
actor_cfg = ActorConfig(component_id="InfluxDBActor-001")
actor = InfluxDBActor(config=actor_cfg)

# 2. Add to a live Trader node
trader: Trader = ...  # built by your engine factory
trader.add_actor(actor)

# 3. Subscribe to the instruments you care about
# (call inside actor.on_start or before trader.start())
from nautilus_trader.model.identifiers import InstrumentId
actor.subscribe_trade_ticks(InstrumentId.from_str("BTC/USDT.BINANCE"))

# Position events are routed automatically via the MessageBus;
# on_event() in InfluxDBActor dispatches PositionChanged internally.

# 4. Start trading
trader.start()
asyncio.get_event_loop().run_forever()
```

## Non-blocking design

All InfluxDB writes and Telegram HTTP calls are scheduled with
`asyncio.ensure_future(...)`.  Exceptions are caught and logged via the
NautilusTrader logger — they **never** raise into the trading loop.

Graceful shutdown is triggered in `on_stop()`, which closes both the
`InfluxDBClientAsync` and `httpx.AsyncClient`.

## Running tests (offline, no live services)

```bash
.venv/bin/python -m py_compile quant_lab/*.py tests/test_influx_actor.py
.venv/bin/python -m unittest tests/test_influx_actor.py -v
```
