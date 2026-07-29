# Security

## Reporting a vulnerability

Open a Security advisory on GitHub (repo Security tab → Advisories → New draft). Do not open a public issue.

## Secure deployment guide (VM deployment of UI/DB)

`deploy/` (Dockerfile, docker-compose, VM script) and `app/` (Grafana provisioning) are a reference deployment for this project's optional infrastructure — see the guides under `docs/guides/` for how they fit together. The TimescaleDB + Grafana services in `docker-compose.yml` use `.env.example` defaults meant for local development; **copying them into production exposes you directly**. Go through this checklist before deploying to any host with a public IP (VM/VPS). It uses GCP as an example, but every item applies regardless of cloud vendor.

### 1. Port binding: localhost/private network only, never `0.0.0.0`

`timescaledb`/`grafana` in `docker-compose.yml` both use `${TSDB_BIND:-127.0.0.1}`/`${GF_BIND:-127.0.0.1}` — **unset, they default to localhost only**. Set `TSDB_BIND`/`GF_BIND` explicitly only when you need access from a private network (e.g. Tailscale).

Check with `docker ps`: the PORTS column should read `127.0.0.1:5432->5432/tcp` or `<private IP>:5432->5432/tcp`. **`0.0.0.0:xxxx->xxxx/tcp` means it's open to the world — fix immediately.**

### 2. Passwords: replace the template defaults at deploy time

`.env.example`'s `POSTGRES_PASSWORD`/`POSTGRES_APP_PASSWORD`/`GF_SECURITY_ADMIN_PASSWORD`/`GRAFANA_PASSWORD` are placeholders (`quant_secret`/`quant_app_secret`/`admin`) — **never deploy with them as-is**. Right after `cp .env.example .env`, replace all four with random strings (e.g. `openssl rand -base64 24`).

Rotating on an existing DB: **`POSTGRES_PASSWORD` in `.env` only takes effect on first container start against an empty volume** — changing it later does nothing. Rotate it directly with `ALTER USER quant WITH PASSWORD '...';`. `POSTGRES_APP_PASSWORD` is different: re-running `timescale_init.sql` against the existing DB (`psql` inside `docker exec`) picks up the new value, since that script uses `ALTER ROLE` when the role already exists. Grafana's admin password behaves like `POSTGRES_PASSWORD` — rotate an existing install with `docker exec <container> grafana cli admin reset-admin-password '...'`, not by editing `.env` and recreating the container.

### 3. Pin image versions, don't use `:latest`

`grafana/grafana:latest` makes the running version unpredictable and untraceable against CVEs. Pin an explicit version (e.g. `grafana/grafana:13.1.1`) so upgrades are deliberate, not passive.

### 4. DB access separation: datasource/research scripts should not use the superuser account

`POSTGRES_USER` is the bootstrap account the official Postgres/Timescale image creates on init, and it's **a superuser by default**. If the Grafana datasource or any externally reachable service connects with it directly, compromising that service's query interface (e.g. Grafana Explore allowing arbitrary SQL) hands the attacker superuser access outright — including `COPY ... TO/FROM PROGRAM`, which runs arbitrary commands on the host.

Mitigation: a separate account with only `SELECT`/`INSERT`/`UPDATE`/`DELETE` for the datasource and general read/write use; reserve the superuser account for schema migrations only. See the ADR under `docs/decisions/` for this least-privilege role split.

### 5. Block containers from reaching the cloud metadata server

Cloud VMs typically expose a service account/instance profile via a link-local address (`169.254.169.254` on GCP and AWS) that hands out credentials with no password required. By default anything on the host that can make a network request can reach it — including any container process, even one with no legitimate need for it. DB/Grafana have no need to reach it, so block it directly:

```bash
# Block all Docker containers from reaching the metadata server
sudo iptables -I DOCKER-USER -d 169.254.169.254 -j DROP
```

To survive a reboot, apply this via a systemd unit (`After=docker.service`). Tools like `iptables-persistent` typically run before Docker creates its `DOCKER-USER` chain, so the rule gets lost — use an explicit oneshot service instead.

### 6. Cloud firewall: confirm management interfaces aren't exposed publicly

Port binding (item 1) is the host-level defense; the cloud firewall/security group is a separate network-level one — both need to be correct, or you're still exposed. For GCP:

```bash
gcloud compute firewall-rules list --format="table(name,sourceRanges.list(),allowed[].map().firewall_rule().list())"
```

Check for any rule opening Grafana (3000) or Postgres (5432) to `0.0.0.0/0`. These ports should never appear in a `0.0.0.0/0` rule — only ports genuinely meant for the public internet (usually none here) should.

## Trading credentials (a separate risk tier)

Keys with real order-placement/signing capability, like `BINANCE_API_KEY`/`SHIOAJI_API_KEY`, carry different risk than the DB/Grafana credentials above, and are deliberately kept out of the `.env` file `deploy/cloud_deploy.sh` syncs (see `.env.secrets.example`). One baseline rule: **disable withdrawal/transfer permission on the API key at the exchange, keep trading permission only** — a leaked key then lets an attacker place unwanted orders but not move funds. Other quant-specific hardening (IP allowlisting, kill switches) isn't documented here yet.

## Resolved security issues

See the repo's Security advisories tab on GitHub — each advisory carries its own timeline and linked fix commit/PR, so this file doesn't duplicate a history list.
