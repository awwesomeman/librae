# Optional infrastructure

Librae's engine can run entirely in memory. This guide explains the reference
implementations that become relevant when you need persistence, broker
execution, notifications, monitoring, or deployment.

## Component map

| Component | Directory | Engine boundary |
|---|---|---|
| TimescaleDB analytics and runtime state | [`db/`](../../db/) | callbacks and `state_store` |
| Broker market data and order routing | [`brokers/`](../../brokers/) | `adapter` and `order_adapter` |
| Telegram notifications | [`notifications/`](../../notifications/) | `notifier` |
| Strategy CLI/config wiring | [`orchestration/`](../../orchestration/) | strategy-owned `run.py` helpers |
| Grafana dashboards | [`app/grafana/`](../../app/grafana/) | queries the reference DB schema |
| Docker and VM scripts | [`deploy/`](../../deploy/) | process/infrastructure examples |

These are replaceable integrations, not required dependencies of the
calculation core. The exact callback and adapter signatures are in the
[Config API and callback reference](../../architecture.md#config-api).

## TimescaleDB

Install the database extra when using the reference writer or live state store:

```bash
pip install "librae[db] @ git+https://github.com/awwesomeman/librae.git@<tag-or-commit>"
```

Create the schema once per database:

```bash
psql "$TIMESCALE_DSN" -f db/timescale_init.sql
```

Normal integrations call the high-level functions in
`db.timescale_writer` and `db.timescale_reader`; upper layers should not issue
ad hoc SQL. In a backtest, `cfg.no_db=True` skips all database writes.

Live execution is different: it requires durable runtime state. With
`cfg.no_db=True`, inject your own durable `state_store`; the in-memory
implementation is intended for deterministic tests only.

## Grafana

Grafana provisioning lives under `app/grafana/provisioning/`. Dashboard JSON
is generated with:

```bash
uv run python -m app.grafana.generate_dashboards
```

The strategy dashboard selects both `run_id` and `account_id`. Equity, PnL,
metrics, and trade events always retain their currency label; it does not
combine accounts, including accounts that share a currency.

Dashboards query the TimescaleDB tables and remain empty until a strategy has
written data. To inspect the panels before running a real strategy, load the
bundled fake rows:

```bash
psql "$TIMESCALE_DSN" -f db/seed_fake_data.sql
```

For a local Grafana instance connected to an existing database:

```bash
cd deploy
docker compose -f docker-compose.local.yml up -d
```

Open `http://localhost:3000`. Credentials and the remote database connection
come from the repository `.env` file as documented in the compose file.

## Brokers

Install only the adapter needed by the execution venue:

| Extra | Adapter | Typical scope |
|---|---|---|
| `crypto-live` | `CryptoAdapter` / CCXT | Crypto |
| `tw-live` | `ShioajiAdapter` | Taiwan stocks and futures |
| `us-live` | `IBKRAdapter` | US stocks and futures |

Market data and execution routing are separate. `data_source` chooses where
bars come from; live execution needs an explicit `broker`,
per-instrument broker override, or injected `order_adapter`. Librae does not
infer an execution venue from a symbol.

Paper trading uses `mode=live` with a broker's paper endpoint. `mode=sim` is a
local shadow simulation and does not exercise acknowledgements, partial fills,
rejections, or broker fees.

## Notifications and custom sinks

The bundled notifier reads Telegram secrets from environment variables and
behavior from `RunConfig.telegram_config`. You can instead inject your own
notifier or persistence callbacks.

See
[LiveTrader callback signatures](../../architecture.md#livetrader-callback-signatures-writing-your-own-db-sink-or-notifier)
for the exact callable contracts. When Grafana is unnecessary, callbacks such
as `on_bar`, `on_order_event`, and `on_heartbeat` can feed an existing
observability stack.

## Deployment examples

The `deploy/`, `app/`, and `scripts/` directories are operational examples,
not engine APIs. They show one Docker/Grafana/VM arrangement and can be used,
replaced, or ignored.

The trade image intentionally combines this engine repository with a separate
`strategies/` repository in the same parent workspace:

```text
workspace/
├── librae/
└── strategies/
```

Run `deploy/build_push.sh` from `librae/`; it fails before invoking Docker when
that sibling repository is absent. The shared image installs the `db`,
`crypto-live`, `tw-live`, and `us-live` extras. Infrastructure-only deployment
via `cloud_deploy.sh` does not copy either application repository; it syncs the
compose file, `db/timescale_init.sql`, Grafana provisioning, and `.env`.

`LiveTrader.run()` is a blocking polling loop. A deployment should run it
under a supervisor appropriate to the environment and must provide durable
state, secret management, monitoring, and recovery procedures before live
capital is enabled. An operator can call `LiveTrader.halt(reason)` to persist a
fail-closed halt and cancel tracked broker orders; resumption requires an
explicit `reset_halt()` after reconciliation.
