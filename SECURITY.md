# Security

## Reporting a vulnerability

Use **Security → Advisories → Report a vulnerability**. Do not disclose
vulnerability details in a public issue.

## Secure deployment guide (VM deployment of the DB)

`deploy/` is reference infrastructure. Review this checklist before using it
on a VM or any host with a public IP.

### 1. Bind management ports to a private interface

`TSDB_BIND` defaults to `127.0.0.1`. Set it only to a private
interface such as a Tailscale address. Check `docker ps`; a `0.0.0.0` mapping
publishes the port on every host interface.

### 2. Replace every placeholder password

Replace every placeholder password and token in `.env.secrets` with an
independent random value, then run `librae doctor`. Which file each variable
belongs in, and what `doctor` checks, is documented once in
[Getting started → Environment variables](docs/getting-started.md#environment-variables).

On an existing deployment, changing `.env.secrets` does not rotate database
roles. Rotate `quant` from a trusted admin session, then rerun
`timescale_init.sql` inside the database container for the two managed
roles.

### 3. Keep container versions explicit

The reference Compose file pins an explicit TimescaleDB version. Review
release notes and CVEs before updating it.

### 4. Keep database roles separate

Use `quant` only for migrations, `quant_app` for application reads and writes,
and `grafana_reader` for the console (`LIBRAE_CONSOLE_DSN`). The console
receives only that reader's password, never the application or admin
credentials.

This prevents the console from changing trading records or using server-side
`COPY PROGRAM`. It does not replace network isolation. The console has no
login of its own and listens on `127.0.0.1` by default; pass
`--server.address` only with a private interface.

### 5. Block containers from reaching the cloud metadata server

Database containers do not need cloud instance credentials. Block
their access to the metadata endpoint:

```bash
# Block all Docker containers from reaching the metadata server
sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
```

Persist the rule with a systemd unit that runs after Docker creates the
`DOCKER-USER` chain.

### 6. Cloud firewall: confirm management interfaces aren't exposed publicly

Also check the cloud firewall. For GCP:

```bash
gcloud compute firewall-rules list --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())"
```

Do not expose the console (8501 by default) or PostgreSQL (5432) to `0.0.0.0/0`.

## Trading credentials (a separate risk tier)

Trading credentials live in per-account `.credentials/*.env` files that
deployment scripts never sync (layout: Getting started → Environment
variables). Disable withdrawal and transfer permissions, restrict keys by
source IP, and use sandbox or paper endpoints for end-to-end tests.
