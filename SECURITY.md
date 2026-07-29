# Security

## Reporting a vulnerability

Use **Security → Advisories → Report a vulnerability**. Do not disclose
vulnerability details in a public issue.

## Secure deployment guide (VM deployment of UI/DB)

`deploy/` and `app/` are reference infrastructure. Review this checklist before
using them on a VM or any host with a public IP.

### 1. Bind management ports to a private interface

`TSDB_BIND` and `GF_BIND` default to `127.0.0.1`. Set them only to a private
interface such as a Tailscale address. Check `docker ps`; a `0.0.0.0` mapping
publishes the port on every host interface.

### 2. Replace every placeholder password

Replace `POSTGRES_PASSWORD`, `POSTGRES_APP_PASSWORD`,
`POSTGRES_GRAFANA_PASSWORD`, `GF_SECURITY_ADMIN_PASSWORD`, and the password in
`TIMESCALE_DSN`. Use independent random values.

On an existing deployment, changing `.env` does not rotate database roles.
Rotate `quant` from a trusted admin session, then rerun `timescale_init.sql`
inside the database container for the two managed roles. Reset an existing
Grafana admin password with `grafana cli admin reset-admin-password`.

### 3. Keep container versions explicit

Both reference Compose files use explicit Grafana and TimescaleDB versions.
Review release notes and CVEs before updating them.

### 4. Keep database roles separate

Use `quant` only for migrations, `quant_app` for application reads and writes,
and `grafana_reader` for dashboard queries. Grafana receives only its reader
password; it does not receive either application or admin credentials.

This prevents Grafana queries from changing trading records or using
server-side `COPY PROGRAM`. It does not replace network isolation.

### 5. Block containers from reaching the cloud metadata server

Database and Grafana containers do not need cloud instance credentials. Block
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

Do not expose Grafana (3000) or PostgreSQL (5432) to `0.0.0.0/0`.

## Trading credentials (a separate risk tier)

Trading credentials stay in `.env.secrets`, which deployment scripts never
sync. Disable withdrawal and transfer permissions, restrict keys by source IP,
and use sandbox or paper endpoints for end-to-end tests.
