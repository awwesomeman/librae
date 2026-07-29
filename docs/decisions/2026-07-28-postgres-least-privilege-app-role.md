# 2026-07-28 — Split database admin, application, and dashboard roles

> Status: implemented

## Background

The reference deployment used the bootstrap `quant` superuser for routine
writes and Grafana queries. Grafana accepts arbitrary datasource queries, so a
shared role exposed both administrative and data-modification privileges.

## Decision

- `quant`: migrations and emergency administration only.
- `quant_app`: application reads and writes through `TIMESCALE_DSN`.
- `grafana_reader`: dashboard queries with `SELECT` only.

Grafana receives only `POSTGRES_GRAFANA_PASSWORD`. The Compose service does not
receive the application or admin database credentials.

`timescale_init.sql` creates or updates the managed roles, enforces
non-administrative role attributes, resets their direct schema privileges, and
sets matching default privileges for future objects created by `quant`.

## Alternatives considered (not adopted)

- **One non-admin role for both callers:** simpler, but lets Grafana alter
  trading records.
- **Demote `quant`:** conflicts with the bootstrap and migration role expected
  by the reference database image.
