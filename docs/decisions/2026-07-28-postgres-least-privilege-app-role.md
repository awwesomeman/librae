# 2026-07-28 — Split DB access into a superuser (migration) role and a least-privilege app role

> Status: implemented (`quant_app` creation/grants are built into `timescale_init.sql`, live from a new DB's first init)

## Background

`POSTGRES_USER=quant` is the bootstrap account the official Postgres/Timescale image creates on init, and it's **a superuser by default**. The Grafana datasource and research scripts' `TIMESCALE_DSN` both used those same credentials directly.

That means anyone who can run SQL through the datasource (e.g. Grafana's Explore) effectively has Postgres superuser access — including `COPY ... TO/FROM PROGRAM`, arbitrary shell execution inside the DB container. One intrusion went exactly this route: Grafana access → SQL via Explore against the superuser datasource → `COPY FROM PROGRAM` for host-level execution. Postgres already supports least-privilege account separation; it just hadn't been implemented.

## Decision

Add a non-superuser role, `quant_app`, granted only `SELECT`/`INSERT`/`UPDATE`/`DELETE` plus default privileges for future tables (`ALTER DEFAULT PRIVILEGES`). No `CREATEROLE`/`CREATEDB`/`SUPERUSER`, so it can't use `COPY ... TO/FROM PROGRAM` (requires superuser or an explicit `pg_execute_server_program` grant).

- `quant` (superuser): schema migrations and emergency admin only, no longer used for routine connections.
- `quant_app`: the Grafana datasource, research scripts (`TIMESCALE_DSN`), and general read/write use, authenticated via the new `POSTGRES_APP_PASSWORD`.

```sql
CREATE ROLE quant_app LOGIN PASSWORD '...';
GRANT USAGE ON SCHEMA public TO quant_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quant_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO quant_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO quant_app;
ALTER DEFAULT PRIVILEGES FOR ROLE quant IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO quant_app;
```

## Implementation notes

This SQL is built into `db/timescale_init.sql` and runs on both a new DB's first init and a manual re-run against an existing DB; it's idempotent (`CREATE` or `ALTER ROLE`, whichever fits). Psql variable substitution (`:'var'`) doesn't work inside `DO $$...$$` blocks, so the password is read via `\getenv` and the `CREATE`/`ALTER ROLE` statement is built as text and dispatched via `SELECT ... \gexec`, rather than the `DO $$ IF NOT EXISTS $$` style used elsewhere in that file.

## Alternatives considered (not adopted)

- **Demote `quant` itself to non-superuser**: the official image's bootstrap account is designed assuming superuser; changing its attributes risks conflicting with the image's init logic or future minor-version upgrades — riskier than adding a separate account.
- **Use `pg_execute_server_program` for fine-grained control instead of fully blocking `COPY PROGRAM`**: would preserve flexibility for controlled host commands, but no current use case needs it — not granting the role at all is simpler and reduces the attack surface more.
