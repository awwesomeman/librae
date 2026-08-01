# Operational runbook

This is the evidence record for the "Live broker" promotion stage in the
[strategy readiness checklist](strategy-readiness.md): a named operator,
alerting, and a rehearsed kill/recovery procedure, before any strategy is
promoted to live capital. It tracks
[issue #86](https://github.com/awwesomeman/librae/issues/86)'s definition of
done. `deploy/`'s Docker/Compose surface is documented separately in
[Optional infrastructure](optional-infrastructure.md); this guide is the
operational procedures layered on top of it, not a restatement of it.

## Operator and escalation

| | |
|---|---|
| Named operator | Jason Pan (repository owner) |
| Escalation path | Single-operator deployment — there is no second responder. If the operator cannot act within the alert's implied urgency (see below), the fail-safe response is: `LiveTrader.halt(reason)` (or kill the process — live mode's durable state makes that safe, see [Restart recovery](#restart-recovery)), then contact the broker's support line directly for any order that halt could not resolve. |
| Reachability | Telegram (bot configured under [Alert delivery](#alert-delivery)) is the paging channel. No on-call rotation exists; do not run live capital during a period the operator cannot monitor Telegram. |

This is intentionally minimal because it is a single-person deployment. If a
second operator is ever added, replace this table with a real escalation
chain before that changes.

## Secrets handling and rotation

Current model (see `.env.example`, `.env.secrets.example`, and
`deploy/cloud_deploy.sh`'s header comment for the rationale):

| Secret class | Lives in | Synced to VM by `cloud_deploy.sh`? | Notes |
|---|---|---|---|
| DB role passwords (`POSTGRES_*`) | `.env` | Yes | Non-trading; rotating them does not touch broker accounts. |
| Telegram bot token/chat id | `.env` | Yes | Revoke via [@BotFather](https://t.me/BotFather) `/revoke`; update `.env` and restart the notifier process. |
| Broker API keys (`BINANCE_*`, `SHIOAJI_*`, IBKR session) | one `.credentials/<account>.env` file per account | Never | Created by hand only on the machine that trades — see `.env.secrets.example`'s header comment. `trade.sh` passes only the explicitly selected file to Docker, never sourced as shell code. |
| Shioaji CA file | `.secrets/` | Never (bind-mounted read-only by `trade.sh`) | |

Rotation procedure (any credential class):

1. Generate the new credential at the provider (exchange API key page,
   Postgres `ALTER ROLE ... PASSWORD`, BotFather `/revoke` + new token).
2. Update the value only on the machine(s) that hold it per the table above
   — `.env` changes propagate via `cloud_deploy.sh`; `.credentials/*.env` and
   `.secrets/` must be edited directly on the trading host over SSH.
3. Restart the affected deployment (`deploy/trade.sh restart <deployment_id>`,
   or the `timescaledb`/`grafana` Compose services for DB passwords).
4. Revoke the old credential at the provider once the new one is confirmed
   working — do not revoke first, or a mid-rotation restart fails closed
   with no way back in.
5. Broker keys: revoke, don't just rotate silently — a leaked trading key
   with withdrawal permission disabled is still an execution risk.

No fixed rotation schedule exists yet; rotate on suspected exposure (a key
committed to a branch, a laptop reformatted, a departing collaborator) and
whenever a credential's provider forces it.

## Alert delivery

**Rehearsal script:** `scripts/rehearse_alerts.py` — drives a real
`LiveTrader` (mode=sim, no broker or DB required) through the production
`_notify`/`_check_staleness` engine code paths, plus
`librae.orchestration.live`'s DB-write-failure wrapper, with a real
`TelegramAdapter` so the alert is an actual message in the configured chat —
not a mocked assertion.

```bash
uv run python scripts/rehearse_alerts.py               # all 3 scenarios
uv run python scripts/rehearse_alerts.py --scenario stale-data
uv run python scripts/rehearse_alerts.py --scenario poll-error
uv run python scripts/rehearse_alerts.py --scenario db-write
```

Requires `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env`. For each
scenario, confirm in the actual Telegram chat: the message arrived, the
title matches, and the operator would understand what to do from the
message alone.

Not covered by the script (exercise separately once a broker sandbox
session is running): a broker-failure alert, which fires through the same
"Poll Error" path when `order_adapter` calls raise — see
[Kill-switch rehearsal](#kill-switch-rehearsal) for a session that exercises
real broker calls.

## Kill-switch rehearsal

**Rehearsal script:** `scripts/rehearse_kill_switch.py` — builds a real
`LiveTrader` via `librae.orchestration.live.build_live_trader()` against
Binance sandbox (Demo Trading, `mode=live`), places one small deep-limit buy
order (far enough below market it will not fill during the rehearsal), then
calls the operator controls from
[Optional infrastructure](optional-infrastructure.md#deployment-examples):

```bash
uv run python scripts/rehearse_kill_switch.py
uv run python scripts/rehearse_kill_switch.py --quantity 0.01 --run-seconds 75
```

`--run-seconds` defaults to 75 (comfortably over 60): on `timeframe=M1` a new
completed bar — the event that makes the strategy place its order — can be
up to a minute away depending on where in the current minute the run
starts.

The script refuses to run unless `BINANCE_SANDBOX=true` — it will not touch
a mainnet account. It rehearses against `ETHUSDT`, not `BTCUSDT`; see the
first finding below for why. If the sandbox account has a non-zero balance
in that symbol already (from earlier testing), pass `--seed-reviewed-state`
to seed a checkpoint matching the account's actual broker state instead of
fighting the exchange to reach literal zero.

Procedure it exercises:

1. `trader.halt("kill-switch rehearsal")` — fails closed immediately and
   cancels tracked broker orders.
2. Operator confirms in the Binance UI that no unexpected position/order
   remains (manual step — the script pauses for this).
3. `trader.reset_halt()` — only after step 2; raises
   `RuntimeError` if any tracked order is still unresolved, which is the
   intended guard rail, not a bug.

**Findings from the 2026-08-01 rehearsal**, worth knowing before running this
again — two were real bugs, fixed in the same session
(`fix(live): keep client_order_id within Binance's 36-char limit`,
`fix(live): register a fresh run before its first checkpoint write`), one is
a still-open limitation this rehearsal routes around:

- **CCXT spot positions never carry an average price, but reconciliation
  requires one (still open).** `_read_broker_positions()` raises
  `ValueError: ... is missing average price` for any non-zero position where
  the broker doesn't return `avg_price` — which Binance spot balances never
  do (CCXT's balance API has no cost-basis field). This makes both the
  first-run bootstrap check and the post-restore reconciliation check
  unconditionally fail for a non-flat CCXT spot position, on *any* account,
  not just one with unclearable dust — worth a follow-up issue against
  `librae/live/engine.py::_read_broker_positions`. Routed around here by
  rehearsing against `ETHUSDT` (this account has zero ETH) instead of
  `BTCUSDT` (which has unclearable dust — see below); `--seed-reviewed-state`
  does *not* work around this, since the engine re-reads live broker state
  and hits the same missing-average-price error regardless of what the
  checkpoint says.
- **`client_order_id` exceeded Binance's 36-character limit for any ordinary
  symbol (fixed).** The readable `strategy-symbol-event-timestamp-sequence`
  id was already 37+ characters for `open`/`close`/`reduce` events on a
  7-character symbol like `BTCUSDT`/`ETHUSDT` — with *zero* characters left
  for `strategy_name`, so no strategy name was ever short enough to avoid
  it. Binance rejected the order outright
  (`Illegal characters found in parameter 'newClientOrderId'`). Fixed by
  falling back to a shortened, hash-suffixed id when the readable form would
  overflow.
- **A fresh live/sim run with a real database crashed on its first
  checkpoint (fixed).** `LiveTrader.__init__` persists a checkpoint
  immediately for any first (non-restored) run with a `state_store`, but
  `build_live_trader()` only registered the run (writing the `backtest_runs`
  row a checkpoint's foreign key depends on) *after* construction returned —
  too late. Every first live/sim run with `database_enabled=True` hit
  `ForeignKeyViolation` on that very first write. `database_enabled=True`
  had no test coverage that actually exercised `build_live_trader()` against
  a database — only the `RunOptions`/CLI config layer was tested, which is
  why this had gone unnoticed. Fixed by adding
  `LiveTrader(on_run_registered=...)`, called with the resolved run_id
  immediately before that first persist.
- **A limit order too far from market gets rejected outright, ambiguously
  (expected behavior, not a bug).** Binance's `PERCENT_PRICE_BY_SIDE` filter
  rejects a bid below `0.5x` the recent average price. When that happened
  mid-rehearsal (before landing on the `0.6x` margin used now), the engine
  logged `Order placement/report FAILED` and correctly treated the order as
  **unresolved** rather than assuming it failed cleanly (`Cannot cancel
  unresolved order`, then `reset_halt()` refused with `cannot reset halt
  while broker orders remain unresolved`) — a real, unplanned exercise of
  the readiness checklist's "placement-ambiguity handling" item, and it held
  up correctly.
- **`reset_halt()` can crash if a position has no cached price yet (still
  open, narrow).** `_calc_account_snapshot()` needs `_last_prices[symbol]`
  for the account's open position and has no fallback — raises an unhandled
  `ValueError` ("no current valuation mark for open position") if
  `halt()`/`reset_halt()` are called before the engine has processed a
  single bar. Only reachable in the narrow window between a fresh
  restored-state deployment starting and its first bar — not fixed here,
  out of this runbook's scope.

## DB backup and restore

**Scripts:** `deploy/db_backup.sh` and `deploy/db_restore.sh`, against the
reference `quant_timescaledb` Compose container.

```bash
cd deploy && docker compose up -d timescaledb   # if not already running
./deploy/db_backup.sh                            # -> ./backups/quant_<UTC timestamp>.dump
./deploy/db_restore.sh ./backups/quant_<timestamp>.dump
```

`db_restore.sh` drops and recreates the entire `quant` database before
restoring into it — exercise it against a scratch container or a
deliberately chosen target, never blind against a live one. It does *not*
restore in place with `pg_restore --clean`: TimescaleDB rejects the `ALTER
TABLE ONLY ... DROP CONSTRAINT` statements `pg_dump`/`pg_restore` generate
around a hypertable's foreign keys (`ONLY option not supported on
hypertable operations`), even though the same dump replays cleanly into an
empty database — this is TimescaleDB's own restore guidance, not a
workaround. An untested backup is not a disaster-recovery procedure; run
this pair at least once before relying on it, and re-run it after any
schema change to `librae/db/timescale_init.sql`.

If a `quant_timescaledb` container's data volume was initialized under an
older checkout's schema, `librae/db/timescale_init.sql` only applies safely
when the database already matches the current revision (see
[Optional infrastructure](optional-infrastructure.md#timescaledb)); drop and
recreate the database against the current schema first rather than
re-running the init script over a stale one.

## Restart recovery

No new script — this rehearses the same live paper session
`scripts/rehearse_kill_switch.py` or `deploy/trade.sh` starts, so it is a
manual procedure on top of the existing runner rather than a separate tool.
(The 2026-08-01 rehearsal used a throwaway driver script instead of
`deploy/trade.sh`, to avoid needing the sibling `strategies/` repo and a
built trade image — same `build_live_trader()`/`database_enabled=True`/
TimescaleDB wiring either way, just invoked directly with `python` instead
of `docker run`.)

1. Start a paper session: `./deploy/trade.sh start <deployment_id>
   <account_id> <currency> <strategy> live <poll_seconds>` (or the
   kill-switch script above, adapted for `database_enabled=True`) against a
   broker sandbox, with the reference TimescaleDB running so state is
   durable.
2. Let it place at least one order and reach a steady polling state.
3. Kill the process hard, mid-cycle: `docker kill <container>` (or
   `kill -9 <pid>` for a non-Docker run) — not `docker stop`, which allows a
   graceful shutdown the recovery test needs to rule out.
4. Restart the same command.
5. Confirm in the logs: `Restored runtime state: key=... run_id=... cycle=...
   orders=N ...`, and specifically that `run_id` matches the killed run and
   `orders` reflects what was actually resting at the broker.
6. Confirm no duplicate order was placed for the cycle that was interrupted
   — cross-check the broker's order history against `active_orders` in the
   restored state and against `order_events` in TimescaleDB.
7. Confirm the state-store lease behaves correctly: attempt to start a
   second instance against the same `config_hash` while the first is
   running: `_state_store.acquire_lease` must refuse it (single-process
   lease guard).

## Rehearsal log

Fill in after each exercise — evidence must be dated and kept with the
strategy release per the readiness checklist, not just "it works."

| Item | Date | Result | Notes |
|---|---|---|---|
| Alert delivery — stale-data | 2026-08-01 | Pass | Real Telegram send, HTTP 200; operator to visually confirm title/content in-chat |
| Alert delivery — poll-error | 2026-08-01 | Pass | Real Telegram send, HTTP 200; operator to visually confirm title/content in-chat |
| Alert delivery — db-write | 2026-08-01 | Pass | Real Telegram send, HTTP 200; operator to visually confirm title/content in-chat |
| Kill switch / reset_halt | 2026-08-01 | Pass | Real order (id `10064760026`, ETHUSDT) placed via `build_live_trader()`, accepted, `halt()` cancelled it (confirmed `status=canceled` directly against Binance), `reset_halt()` succeeded. Required the two fixes above; see findings |
| DB backup/restore | 2026-08-01 | Pass | `db_backup.sh` then `db_restore.sh` against `quant_timescaledb` on the current schema; all 13 tables present after restore |
| Restart recovery | 2026-08-01 | Pass | Real order (id `10069065389`, ETHUSDT) placed on Binance sandbox with `database_enabled=True` (real TimescaleDB), process `kill -9`'d mid-cycle, restarted: log showed `Restored runtime state: ... orders=1` matching the resting order, no duplicate order placed afterward (confirmed 1 open order on Binance throughout). Item 7 (single-process lease guard) not exercised this round |
