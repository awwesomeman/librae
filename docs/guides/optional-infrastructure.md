# Optional infrastructure

Librae's engine can run entirely in memory. This guide explains the reference
implementations that become relevant when you need persistence, broker
execution, notifications, monitoring, or deployment.

## Component map

| Component | Directory | Engine boundary |
|---|---|---|
| TimescaleDB analytics and runtime state | `librae/db/` | callbacks and `state_store` |
| Local research artifacts | `librae/artifacts.py` | format-neutral manifest and tables |
| Broker market data and order routing | `librae/brokers/` | `adapter` and `order_adapter` |
| Telegram notifications | `librae/notifications/` | `notifier` |
| Strategy CLI/config wiring | `librae/orchestration/` | strategy-owned `run.py` helpers |
| Grafana dashboards | `librae/app/grafana/` | queries the reference DB schema |
| Docker and VM scripts | `deploy/` | process/infrastructure examples |

These are replaceable integrations, not required dependencies of the
calculation core. The exact callback and adapter signatures are in the Config
API and callback reference in `architecture.md`.
Third-party packages should import engine-facing contracts from
`librae.integrations`.
`librae.orchestration.live.build_live_trader()` is the convenience factory for the
repository implementations. Construct `LiveTrader` directly when injecting
different adapters, callbacks, notifier, or durable state.

Integration registration is deliberately explicit. Librae does not scan
installed modules, namespace packages, or entry points, so installing an
unused provider cannot execute its code or break the base engine. Reference
implementations use regular `librae.*` packages, and no repository-level
compatibility aliases are provided. The ownership decision is recorded in
`docs/decisions/2026-07-30-integration-discovery-and-packaging.md`.

## TimescaleDB

Install the database extra when using the reference writer or live state store:

```bash
pip install "librae[db] @ git+https://github.com/awwesomeman/librae.git@<tag-or-commit>"
```

The reference Compose service initializes an empty database automatically.
`timescale_init.sql` defines the current schema and does not migrate an older
one. Re-running it is supported only when the database already matches the
current revision, for example when refreshing role grants:

```bash
docker exec -i quant_timescaledb psql -U quant -d quant < librae/db/timescale_init.sql
```

When a revision changes the schema, recreate disposable development data or
perform an explicit operator-owned migration before running the new revision.
For a database outside the reference Compose setup, run the script with a
database-owner connection and set `POSTGRES_APP_PASSWORD` and
`POSTGRES_GRAFANA_PASSWORD` in that `psql` process. `TIMESCALE_DSN` belongs to
the non-admin `quant_app` role and must not be used for schema administration.
It is the host-side endpoint used by local tools. The reference `trade.sh`
instead reads `TRADE_TIMESCALE_DSN` and passes it into the trade container as
`TIMESCALE_DSN`; that value uses the `quant_timescaledb` service identity on
`quant_network`, not container loopback.

Normal integrations call the high-level functions in
`librae.db.timescale_writer` and `librae.db.timescale_reader`; upper layers should not issue
ad hoc SQL. The repository runner skips database writes when
`RunOptions.database_enabled` is false.

`timescale_writer`'s `instrument_type` params are validated in Python
against `librae.config.symbols.ALLOWED_INSTRUMENT_TYPES` before any SQL
runs — fails fast instead of relying on the `CHECK` constraint at `INSERT`.

Backtest database reuse is disabled unless the caller supplies
`backtest_revision` through CLI/YAML orchestration and passes the same value to
`save_strategy_results()` or `save_signal_results()`. The value is an opaque,
immutable fingerprint owned by the strategy project and must change when
either strategy code or input data changes. Librae combines it with
`config_hash`; it does not infer Git state or hash the caller's dataset.
`--force` requires a revision and replaces only the run with that combined
cache identity.

Adding `backtest_revision` and `backtest_cache_key`, changing `config_hash` to
a non-unique index, and adding the cache-key unique index are schema changes.
An existing database must be recreated or migrated explicitly before this
revision is used; re-running `timescale_init.sql` cannot replace the old unique
index in place.

With repository database wiring disabled, local research remains free of
implicit persistence. Call
`build_backtest_artifact()` or `build_market_data_artifact()` explicitly, then
use pandas to write the returned tables to Parquet, SQLite, DuckDB, or another
format. See `docs/guides/local-artifacts.md`. Librae defines the table and
manifest shape; the caller owns file paths, overwrite policy, transactions,
partitioning, and retention.

Live execution is different: it requires durable runtime state. When
constructing `LiveTrader` directly, inject your own durable `state_store`; the
in-memory implementation is intended for deterministic tests only.

## Grafana

Grafana provisioning lives under `librae/app/grafana/provisioning/`. Dashboard JSON
is generated with:

```bash
uv run python -m librae.app.grafana.generate_dashboards
```

The strategy dashboard selects both `run_id` and `account_id`. Equity, PnL,
metrics, and trade events always retain their currency label; it does not
combine accounts, including accounts that share a currency.

Dashboards query the TimescaleDB tables and remain empty until a strategy has
written data. To inspect the panels before running a real strategy, load the
bundled fake rows:

```bash
psql "$TIMESCALE_DSN" -f librae/db/seed_fake_data.sql
```

For a local Grafana instance connected to an existing database:

```bash
cd deploy
docker compose --env-file ../.env -f docker-compose.local.yml up -d
```

Open `http://localhost:3000`. Credentials and the remote database connection
come from the repository `.env` file as documented in the compose file.

## Brokers

Install only the adapter needed by the execution venue:

| Extra | Adapter | Typical scope |
|---|---|---|
| `crypto-live` | `CryptoAdapter` / CCXT | Crypto |
| `tw-live` | `ShioajiAdapter` | Taiwan futures; stock account routing is not currently supported |
| `us-live` | `IBKRAdapter` | US stocks and futures |

Market data and execution routing are separate. `data_source` chooses where
bars come from; live execution needs an explicit `broker`,
per-instrument broker override, or injected `order_adapter`. Librae does not
infer an execution venue from a symbol.

For an IBKR gateway on the Docker host, set `IBKR_HOST` to
`host.docker.internal`; the reference trade script adds the Linux host-gateway
mapping. For a gateway container on `quant_network`, use its service name.
Container loopback is rejected because it would address the trade container
itself. These settings establish routing only and do not certify an IBKR
session or order lifecycle.

Adapters and credentials can be imported from `librae.brokers` for
caller-owned research or custom wiring. See
`docs/guides/external-data.md` for the polling callable, DB warm-up, and
third-party factor boundaries.

A third-party package can register explicit factories in the strategy-owned
runner without modifying Librae:

```python
from my_broker import MyBroker
from librae.orchestration.live import build_live_trader

trader = build_live_trader(
    strategy,
    feature_fn,
    config=config,
    adapter_factories={
        "my_broker": lambda *, trading: MyBroker(trading=trading),
    },
    notifier=my_notifier,
    state_store=my_state_store,
    runtime_revision=my_runtime_revision,
)
```

Use the same non-empty name in `instrument_overrides.<symbol>.data_adapter`
or `broker`. Registration is explicit; installing a package does not execute
or discover plugin code automatically.

Third-party packages can validate fixtures without connecting to a venue:

```python
from librae.testing import (
    normalize_broker_report,
    validate_bar_data,
    validate_order_adapter,
)

validate_order_adapter(adapter)
validate_bar_data(sample_bars)
normalized = normalize_broker_report(sample_request, sample_broker_report)
```

Paper trading uses `mode=live` with a broker's paper endpoint. `mode=sim` is a
local shadow simulation and does not exercise acknowledgements, partial fills,
rejections, or broker fees.

## Notifications and custom sinks

Install `librae[telegram]` to use the bundled notifier. It reads Telegram
secrets from environment variables and behavior from
`RunOptions.telegram_config`. Database persistence and notifications are
independent options. You can instead inject your own notifier or persistence
callbacks without installing that extra.

See the LiveTrader callback signatures in `architecture.md` for the exact
callable contracts. When Grafana is unnecessary, callbacks such
as `on_bar`, `on_order_event`, and `on_heartbeat` can feed an existing
observability stack.
`on_funding_cash_flow` receives each applied shadow-simulation funding event;
live broker balances remain authoritative.

## Deployment examples

The `deploy/`, `librae/app/`, and `scripts/` directories are operational examples,
not engine APIs. They show one Docker/Grafana/VM arrangement and can be used,
replaced, or ignored. Read `SECURITY.md` before
deploying them to a host with a public IP.

The `deploy/` and `scripts/` trees are checkout-only and are not installed in
the Python wheel. The packaged distribution does include the SQL schema and
Grafana provisioning resources under `librae.*` for caller-owned
infrastructure. The reference VM flow below requires a Librae checkout on the
build machine, Bash for the local scripts, and a compatible Linux target; it
is not part of the OS-independent Python package contract.

The reference builder combines this engine checkout with caller-owned strategy
source. `TRADE_STRATEGY_PATH` selects that source directory; relative paths
resolve from the Librae checkout. Its default, `../strategies`, gives this
convenient layout but is not required:

```text
workspace/
├── librae/
└── strategies/
```

The selected directory is mapped to the container's `strategies` import
package. Each deployable name has one explicit entry contract:

```text
<TRADE_STRATEGY_PATH>/
└── my_strategy/
    ├── __init__.py
    ├── run.py
    └── config.yaml
```

`trade.sh` runs `python -m strategies.my_strategy.run`. The packaged
`config.yaml` is the default, while `--config <path>` mounts an
operator-selected configuration read-only and passes it through the runner's
existing `--config` option. Strategy helpers may live beside the required
files. The source directory does not have to use Git; it should have its own
`.dockerignore` when it contains files that must not enter the image.

Run `deploy/build_push.sh` from `librae/`; it fails before invoking Docker when
the selected source directory is absent. The shared image installs the
`calendars`, `cli`, `db`, `crypto-live`, `telegram`, `tw-live`, and `us-live`
extras at the exact versions selected by the checked-in `uv.lock`.
`deploy/Dockerfile` uses `uv sync --locked`, so a build fails instead of
resolving new versions when `pyproject.toml`, those extras, and the lock
disagree. It also pins the Python base and `uv` installer by multi-platform
manifest digest. A deliberate base, installer, dependency, or extras refresh
therefore appears in review. Both the local build in `trade.sh` and the
registry build in `build_push.sh` use this same Dockerfile and frozen
selection.

Configure registry mirrors, HTTP proxies, and BuildKit caches on the Docker
builder in the normal way for the environment. Configure an alternative
Python package index through the standard `UV_DEFAULT_INDEX`, `UV_INDEX`, or
`UV_FIND_LINKS` environment variables; both build scripts forward only the
values that are set. These are Docker build arguments and must contain only
non-secret mirror or link settings. For an authenticated index, set
`UV_CONFIG_FILE` to a local `uv.toml`; the scripts pass that file to
`deploy/Dockerfile` as a temporary BuildKit secret. Keep the file outside
version control. The repository does not embed an index URL, credentials, or
TLS exceptions.

Infrastructure-only deployment via `cloud_deploy.sh` does not copy either
application repository; it syncs the compose file, `librae/db/timescale_init.sql`,
Grafana provisioning, and `.env`.

This combined-source builder is optional. A caller-owned image may instead
install a pinned Librae distribution and copy its own strategy package, as
long as it provides the `strategies.<name>.run` module invoked by `trade.sh`.
The combined-source builder copies strategy source but does not discover or
install strategy-specific requirements. A strategy with additional
dependencies must provide a caller-owned final image or an explicit extension
layer that installs its own frozen dependencies.
The final image digest identifies the selected image bytes, independently of
the stable `deployment_id` that identifies one running process.

`TRADE_IMAGE` names the registry repository used only by `build_push.sh`. The
script publishes a Librae-revision candidate tag and prints
`TRADE_IMAGE_REF=<repository>@sha256:<digest>`. Copy that exact value into the
target's operator-managed environment before starting a registry deployment.
`trade.sh` rejects mutable tags and uses the same digest-qualified reference
for pull, database preflight, and the running container. It does not edit the
environment automatically. After selecting either a registry or locally built
image, it reads that image's immutable Docker image ID, records it as a
container label, and passes it to the runner as `--runtime-revision`.

Keep the previous digest when promoting a new image. Rollback selects that
previous `TRADE_IMAGE_REF`; because the checkpoint retains the accepted image
ID, selecting the matching old image can restore it without rewriting state.
A different image remains blocked until the operator completes the migration
or flat-account reset procedure below.

### Reference VM flow

The target must provide Bash, rsync, and Docker Compose. Its SSH account must
permit local TCP forwarding from the caller to the target's `localhost:3000`.
The deployment fails when any required capability is unavailable.

1. On the build machine, set `TRADE_STRATEGY_PATH` and `TRADE_IMAGE` in the
   Librae checkout's `.env`.
2. Run `deploy/build_push.sh` and copy its printed `TRADE_IMAGE_REF` into the
   `.env` that `cloud_deploy.sh` will sync.
3. Run `deploy/cloud_deploy.sh <user>@<host>` to sync infrastructure files and
   start TimescaleDB and Grafana. It does not start a strategy.
4. Create account configuration files and copy `.env.secrets.example` to one
   `.credentials/<account>.env` file per live account directly on the VM.
   Deployment scripts never sync broker credentials.

   ```bash
   mkdir -p .credentials
   cp .env.secrets.example .credentials/ibkr-main.env
   chmod 600 .credentials/ibkr-main.env
   ```

5. On the VM, start each deployment with a stable id, the account id and
   currency declared by its selected configuration, and a strategy name:

   ```bash
   ./deploy/trade.sh start momentum-paper paper USD momentum sim 60 \
       --config configs/paper.yaml

   ./deploy/trade.sh start momentum-live ibkr_main USD momentum live 60 \
       --config configs/ibkr-main.yaml \
       --credentials .credentials/ibkr-main.env
   ```

   Use `live` only after broker and checkpoint procedures are satisfied.

Each selected configuration must explicitly bind the deployment account:

```yaml
strategy:
  account:
    account_id: ibkr_main
    currency: USD
    initial_cash: 100000
```

The image preflight rejects a missing or different `account_id` or `currency`
before an existing container is replaced. Container names derive from
`deployment_id`, so the same image and strategy can run for multiple accounts:

```bash
./deploy/trade.sh start momentum-main ibkr_main USD momentum live 60 \
    --config configs/ibkr-main.yaml \
    --credentials .credentials/ibkr-main.env

./deploy/trade.sh start momentum-ira ibkr_ira USD momentum live 60 \
    --config configs/ibkr-ira.yaml \
    --credentials .credentials/ibkr-ira.env
```

Both containers receive the same `TRADE_TIMESCALE_DSN` and use the same
TimescaleDB. Their run and account identifiers keep persisted facts distinct.
The Docker label check rejects an obvious duplicate before replacement; the
durable `live-account:<account_id>` database lease is authoritative across
different strategies, configurations, and launch paths.

`trade.sh start` reports success only after the runner publishes readiness.
The repository's `build_live_trader()` wiring does this after state restore,
durable ownership, and startup broker reconciliation. A custom runner must write
`${LIBRAE_READY_TOKEN}:<run_id>:<32-character-lowercase-hex-generation>` to
`LIBRAE_READY_FILE` after completing the same startup checks. An absent,
malformed, or stale marker does not make the deployment ready.

Use the lifecycle commands without relying on shell process memory:

```bash
./deploy/trade.sh inspect momentum-main
./deploy/trade.sh restart momentum-main
./deploy/trade.sh stop momentum-main
./deploy/trade.sh stop momentum-main --force
```

Normal stop sends SIGTERM, waits up to `TRADE_STOP_TIMEOUT_SECONDS`, and
preserves the stopped container for inspection or restart. `--force` is the
explicit SIGKILL path. Starting the same deployment again validates the new
image and configuration first, then gracefully stops and removes the old
container. A failed new launch remains failed for diagnosis; the script does
not blindly reactivate the old revision. `trade.sh stop --all` applies the same
graceful behavior to all containers carrying the Librae managed label.
Repository CI validates the multi-platform image, digest-only handoff,
infrastructure deployment and repeat deployment, strategy lifecycle, and
durable account lease against disposable Linux targets. It does not certify
registry authentication, image signing, cloud hardening, broker APIs, or real
credentials.

`cloud_deploy.sh` reports file transfer, Compose startup, TimescaleDB
readiness, schema loading, Grafana readiness, and dashboard publication as
separate stages. Readiness waits are bounded to 180 seconds by default; set
`CLOUD_DEPLOY_TIMEOUT_SECONDS` to another positive integer for a slower host.

Before the first account-specific live launch, stop any container created by
the older `quant_live_<strategy>` naming contract. `trade.sh` rejects an
unlabeled legacy live container because it cannot prove which account that
process owns.

`LiveTrader.run()` is a blocking polling loop. A deployment should run it
under a supervisor appropriate to the environment and must provide durable
state, secret management, monitoring, and recovery procedures before live
capital is enabled. An operator can call `LiveTrader.halt(reason)` to persist a
fail-closed halt and cancel tracked broker orders; resumption requires an
explicit `reset_halt()` after reconciliation.

The [operational runbook](operational-runbook.md) is where the named
operator, secret rotation, and rehearsed alert/kill-switch/backup/restart
procedures for a given deployment are recorded.

### Development checkpoint compatibility

The relevant identities are intentionally not interchangeable:

| Identity | Purpose |
|---|---|
| `config_hash` | Resolved engine configuration |
| `runtime_revision` | Caller-owned code or image identity stored in a live checkpoint |
| Image digest or image ID | Selected deployment artifact; `trade.sh` uses the actual image ID as `runtime_revision` |
| `deployment_id` | Stable container/process slot |
| `run_id` | Engine run restored from the checkpoint |

The live runtime requires a non-empty `runtime_revision`. It accepts only the
current checkpoint schema and an exact revision match; it does not convert,
discard, adopt, or overwrite incompatible state. The compatibility check runs
before broker reconciliation, order lookup, or submission. A
shadow-simulation checkpoint may be discarded and recreated.

For live deployments, treat a runtime revision change as an operational
migration:

1. Stop the existing runner.
2. Reconcile broker positions, open orders, and balance against the stored
   state.
3. If exposure or active orders remain, keep the matching revision or close
   them through an explicit operator procedure; do not discard the checkpoint.
4. Start the new revision with fresh state only after the broker account is
   confirmed flat, and retain the old checkpoint for audit.

A pre-v16 checkpoint requires explicit external migration or removal before
this version can run. Re-running database initialization does not transform a
stored JSON checkpoint.

A configuration-shape change may also produce a different `config_hash` and
therefore a different `state_key`, making the new runner appear to have no
matching checkpoint. Startup reconciliation remains a safety check, not a
replacement for the operator procedure above.

For a direct non-container launch, the caller must provide an equivalent
immutable identity:

```bash
python -m my_strategy.run --mode live --poll-seconds 60 \
    --runtime-revision strategy-package-sha256-or-clean-source-revision
```

A clean full commit SHA is acceptable when it identifies all strategy and
Librae code used by the process. A dirty checkout needs a content or package
digest instead. Librae deliberately does not inspect the checkout or choose
this identity.
