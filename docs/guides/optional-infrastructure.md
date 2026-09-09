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
`timescale_init.sql` records the current revision in
`librae_schema_revision`; it refuses to stamp or migrate an older database.
Both schema commands connect as the role that owns the tables, which
`TIMESCALE_DSN` deliberately is not — `quant_app` holds DML only and cannot
`ALTER` a table or write `librae_schema_revision`. Set `TIMESCALE_ADMIN_DSN`
in `.env.secrets` on the machine that migrates; without it the commands fail
rather than falling back.

Preflight inspects twice, and passes only if both agree: once as the owner,
which is the role the migration itself will run as, and once through
`TIMESCALE_DSN` as the application role, which is the role the engine reads
with. Both halves are needed because `information_schema.columns` is
permission-filtered — a table the owner sees every column of shows none to a
role that was never granted it, so an owner-only pass could report `current`
on a database the engine then refuses at startup.

Before deploying a build against an existing database, run the read-only
preflight:

```bash
librae db preflight
```

A non-current result prevents run registration and live checkpoint restore.
For the supported legacy revision, first stop writers, take and verify a
backup, then run the ordered SQL migration:

```bash
librae db migrate
librae db preflight
```

Each migration runs in the same transaction as an advisory schema lock. A
failed statement rolls the revision and DDL back together; fix the reported
cause and rerun. A repeated successful run is a no-op. Newer, unsupported-old,
unknown, and partially applied schemas fail closed instead of being guessed.
Database backups remain the rollback boundary: migrations are forward-only,
so restore the pre-upgrade dump together with the matching application image.

Re-running the bootstrap is supported only when the database already matches
the current revision, for example when refreshing role grants:

```bash
docker exec -i quant_timescaledb psql -U quant -d quant < librae/db/timescale_init.sql
```

For an unversioned schema older than the one recognized by the migration
command, recreate disposable development data or write an operator-reviewed
migration before running the new revision.
The subscription-identity schema adds `backtest_runs.primary_subscriptions`,
`ohlcv.calendar_id`, `ohlcv.available_at`, and the calendar dimension on
`ohlcv_coverage_ranges`; it also replaces the OHLCV unique/index keys. Those
columns must be backfilled from auditable source metadata before constraints
and indexes are changed. Do not infer missing calendar or instrument type from
the symbol string. Runs whose exact six-field identities cannot be recovered
must be marked explicitly with `primary_subscriptions=[]` as legacy records;
`load_ohlcv(run_id=...)` rejects them and requires recreation or an explicit
operator-owned migration instead of reading a mixed dataset. New writes still
require a complete non-empty identity list. Re-running `timescale_init.sql` is
not that migration. Its create-if-not-exists statements also do not add columns
or replace constraints on an existing table. The reference live factory
therefore treats `backtest_runs` registration as a startup precondition: if the
foreign-key parent cannot be written, construction fails before polling or
order execution and emits a distinct `DB Startup Failed` alert when a notifier
is configured. That alert means persistence startup failed, not that a run
started without a dashboard row. Other analytics callbacks remain best-effort
after successful registration.

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
OHLCV read/write and coverage helpers accept a `MarketDataSubscription`
instead of a collection of optional scalar filters. `load_ohlcv(as_of=...)`
filters on `available_at`, so a DB-backed warmup cannot observe a bar merely
because its start label is inside the requested range.
OHLCV upserts treat `available_at` as a row-version clock: only a strictly
later value replaces the stored OHLCV row. Equal versions are idempotent and
older replays are ignored, so values and availability never come from
different versions.
Live `on_ohlcv` is a best-effort audit callback over the fetched-history
window. After a restart, rows at or behind the durable event watermark can be
replayed without re-running strategy or execution, so a custom sink must be
idempotent on the exact subscription identity plus `(ts, available_at)`.
Delivery failures are not guaranteed to retry; the callback is not a durable
outbox or acknowledgement protocol. The built-in Timescale sink tolerates
duplicate delivery through the same strictly-newer row-version upsert policy.

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

`broker_orders.cancel_requested` is a schema change on the same terms. It
records that the engine decided to cancel an order, which is separate from the
broker having acknowledged one: `status` carries only what the broker reported
and stays inside its `CHECK` vocabulary, so an engine-owned intent cannot be
expressed by adding a value there. The flag is checkpointed before the cancel
call, which is what lets a restart resume an unresolved cancellation rather
than lose it.

`config_hash` changed representation on the same terms, and it reaches further
than the database. Hash-included mappings are now encoded with explicit type
tags instead of a `default=str` fallback, and a timeframe is hashed in its
canonical form, so a configuration that hashed one way before this revision
hashes another way after it — including one written only in ccxt timeframe
form. A backtest cache entry keyed on the old hash simply stops matching and
the run recomputes.

A live or sim deployment needs an operator decision before a configuration
identity change. Sim uses `sim:config_hash`; live additionally includes the
adapter-observed execution identity. Under a new key the
runner finds no checkpoint at the new key and starts from an empty book
rather than refusing, so a restart mid-position would lose its positions,
in-flight orders and halted flag while the venue still holds them. Stop each
deployment flat before upgrading, or copy the checkpoint row to the new
`state_key` first. This is the same class of decision as the runtime-revision
mismatch the runner does refuse — that guard lives inside checkpoint restore
and cannot see a key that no longer resolves.

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

That is now enforced rather than advised. A store declares
`restart_durable: bool` — whether its writes survive the process — and live
startup refuses anything that does not declare `True`. The persistence methods
alone cannot express this, since a dictionary satisfies them, so a store that
declares nothing is treated as not durable: silence is not a claim. The
reference `TimescaleLiveStateStore` declares it; `MemoryLiveStateStore` does
not, and takes an explicit `restart_durable_for_tests=True` for suites that
exercise the live path without a database. Reaching order-capable startup with
no recovery state fails only after a crash, with the local book gone and the
broker still holding positions, which is why this fails closed at
construction.

## Grafana

Grafana provisioning lives under `librae/app/grafana/provisioning/`. Dashboard JSON
is generated with:

```bash
uv run python -m librae.app.grafana.generate_dashboards
```

The strategy dashboard selects `run_id`, `account_id`, and `symbol`. Equity,
PnL, metrics, and trade events always retain their currency label; it does
not combine accounts, including accounts that share a currency. Price Trend
and Entry/Exit Signals show one symbol at a time — Open Positions and
Portfolio Exposure cover the full multi-symbol/portfolio state.

The account overview dashboard answers the other question: what every run in
one account is doing right now. It shows one latest-state row per run —
equity, exposure, concentration, open-position count, and last heartbeat —
for a single currency and account over the selected time range, and its
currency and account variables are single-select so a query cannot span
either. Nothing is summed across rows: combined PnL, Sharpe, drawdown, and
reporting-currency conversion stay caller-owned, per `architecture.md`. Open
positions are reconstructed at each run's own equity timestamp rather than at
"now", so a row never mixes moments. See
[the dashboard-scope ADR](../decisions/2026-09-07-account-overview-is-a-separate-dashboard.md).

OHLCV panels use the same exact six-field `primary_subscriptions` identity as
the Python reader and never widen a query to make legacy data appear. A legacy
run marked with `primary_subscriptions=[]` therefore has no price data in the
strategy or signal dashboard; recreate the run or explicitly migrate its
metadata from an auditable source.

Dashboards query the TimescaleDB tables and remain empty until a strategy has
written data. To inspect the panels before running a real strategy, load the
bundled fake rows:

```bash
psql "$TIMESCALE_DSN" -f librae/db/seed_fake_data.sql
```

For a local Grafana instance connected to an existing database:

```bash
cd deploy
docker compose --env-file ../.env --env-file ../.env.secrets -f docker-compose.local.yml up -d
```

Open `http://localhost:3000`. The remote database connection comes from
`.env`; admin/Grafana passwords come from `.env.secrets` (copy
`.env.secrets.example` and fill in the Docker Compose infra secrets section)
as documented in the compose file.

The Signal Monitor dashboard's forward-return/MFE/MAE panels (Cumulative
Signal Return, Rolling Mean Return, Rolling Edge Ratio) recompute that logic
directly in SQL rather than calling `librae.compute_signal_outcomes()` —
Grafana runs against Postgres and cannot call Python. This is a deliberate,
independent implementation for a live/rolling-window dashboard, not
duplication to be unified with the offline Python path; keep both correct
under review rather than trying to merge them.

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
itself. Standard IBKR ports identify paper (7497/4002) or production
(7496/4001). When a proxy or container publishes a custom port, set
`IBKR_ENVIRONMENT=paper` or `production` explicitly; an ambiguous environment
fails before state lookup. These settings establish routing only and do not
certify an IBKR session or order lifecycle.

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

Pass `adapter=` to `normalize_broker_report` when the adapter declares a compact
`broker_client_order_id` form, so the client id check matches live execution.

Every live order adapter must expose `execution_identity() ->
ExecutionIdentity`. The value identifies the broker, environment class
(`sandbox`, `paper`, or `production`), a sanitized endpoint/venue label, and an
opaque account fingerprint derived from authenticated adapter facts. A custom
adapter that cannot determine those facts fails before checkpoint restore or
run registration. Do not return credentials, URLs containing user info, or a
raw account number.

Paper trading uses `mode=live` with a broker's paper endpoint and therefore
still permits broker-confirmed orders. `mode=sim` is the supported no-order
path and does not exercise acknowledgements, partial fills, rejections, or
broker fees. `--mode live --dry-run` is rejected because `--dry-run` only
suppresses persistence and notifications; it was never an order kill switch.

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
source. `TRADE_STRATEGY_PATH` selects that source directory and is required —
there is no default. Relative paths resolve from the Librae checkout, so a
co-located layout like this works, but librae and the strategy source do not
need to sit next to each other:

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
application repository; it syncs the compose file, the schema bootstrap and
ordered migration SQL under `librae/db/`, Grafana provisioning, and `.env`.

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

The steps below reach Grafana and TimescaleDB over an SSH tunnel. On a VPS
with Tailscale, `GF_BIND`/`TSDB_BIND` can instead be set to the VPS's
Tailscale IP for direct access without a tunnel; avoid `0.0.0.0`, which has
been used as an intrusion entry point before.

1. On the build machine, set `TRADE_STRATEGY_PATH` and `TRADE_IMAGE` in the
   Librae checkout's `.env`.
2. Run `deploy/build_push.sh` and copy its printed `TRADE_IMAGE_REF` into the
   `.env` that `cloud_deploy.sh` will sync.
3. Before the first deploy to a new VM, create `.env.secrets` directly on the
   VM and fill in every value the template asks for — `cloud_deploy.sh`
   never syncs this file, and `trade.sh` (used in step 6) requires it:

   ```bash
   scp .env.secrets.example <user>@<host>:quant-deploy/.env.secrets
   ssh <user>@<host> "vi quant-deploy/.env.secrets"   # fill in real values
   ```

   On the build machine, run `librae doctor` in the checkout first
   ([Getting started → Environment variables](../getting-started.md#environment-variables)).
   `cloud_deploy.sh` independently refuses to sync a `.env` that assigns a
   secret.

4. Run `deploy/cloud_deploy.sh <user>@<host>` to sync infrastructure files and
   start TimescaleDB and Grafana. It does not start a strategy.
5. Create account configuration files and copy `.env.secrets.example` to one
   `.credentials/<account>.env` file per live account directly on the VM.
   Deployment scripts never sync broker credentials.

   ```bash
   mkdir -p .credentials
   cp .env.secrets.example .credentials/ibkr-main.env
   chmod 600 .credentials/ibkr-main.env
   ```

6. On the VM, start each deployment with a stable id, the account id and
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
| `execution_identity` | Adapter-observed broker, environment, endpoint/venue, and opaque account fingerprint |
| `runtime_revision` | Caller-owned code or image identity stored in a live checkpoint |
| Image digest or image ID | Selected deployment artifact; `trade.sh` uses the actual image ID as `runtime_revision` |
| `deployment_id` | Stable container/process slot |
| `run_id` | Engine run restored from the checkpoint |

The live runtime requires a non-empty `runtime_revision` and execution
identity. It accepts only the current checkpoint schema and exact revision and
execution-identity matches; it does not convert,
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

Two independently versioned things are easy to confuse here. The database
schema revision lives in `librae_schema_revision` and is upgraded by `librae
db migrate`. The **checkpoint document version** (`_STATE_SCHEMA_VERSION` in
`librae/live/state.py`) is stored as `schema_version` inside the JSON in
`execution_runtime_state.state`, is compared for exact equality on load, and
**nothing migrates it automatically** — running database initialization or
`librae db migrate` does not transform stored checkpoint JSON.

Any version other than the one this build writes is rejected outright rather
than silently defaulted, and the refusal names the versions involved and the
procedure. That is deliberate, not a missing feature: the document holds
positions, cash, in-flight orders and the halted flag, so a defaulted field
would let the local book disagree with the broker. Most changes are not
safely defaultable — forgetting a cancel intent or a bar's already-filled
quantity diverges silently, which is the failure this prevents.

A checkpoint may also carry unacknowledged OHLCV audit rows, so migrating one
externally means carrying that queue too or accepting that those rows are lost
from the audit table; they are analytics, not the book.

The procedure is the same whichever version is stored. Stop flat and start a
new checkpoint, or externally migrate the JSON document and key only after
reconciling its positions and active orders to the same authenticated broker
account. What each version changed is recorded in the commit that bumped it,
which is why it is not restated here.

A configuration, broker environment, endpoint, or authenticated account change
produces a different `state_key`, making the new runner appear to have no
matching checkpoint. On CCXT venues, which expose no portable account
identifier, the account fingerprint is derived from the API key, so **rotating
an exchange API key also changes the live `state_key`** even though the account
is unchanged; rotate only against a flat book, using the same procedure. Startup
position/open-order reconciliation remains a separate safety check, not a
replacement for the operator procedure above. `--reset-state` clears simulation
checkpoints only and refuses live ones outright, because a `config_hash` alone
cannot select between paper and production state.

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
