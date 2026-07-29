# 2026-07-28 — Make Grafana port binding configurable

> Status: implemented
> Note: `GF_BIND` is now live in `docker-compose.yml`, following the same `TSDB_BIND` pattern already established for the `timescaledb` service (see the earlier ADR under `docs/decisions/` introducing `TSDB_BIND`)

## Background

`docker-compose.yml`'s `grafana` service was always `ports: ["3000:3000"]`, with no bind interface specified. Docker defaults an IP-less port mapping to `0.0.0.0`. The `timescaledb` service already restricted its bind interface with `${TSDB_BIND:-127.0.0.1}`, but `grafana` never matched it.

Consequence: on one VM deployment, this unrestricted port 3000, combined with a gap in the cloud firewall rules, exposed Grafana directly to the public internet and became the entry point for an intrusion.

## Decision

Add `GF_BIND`, defaulting to `127.0.0.1`, following the same pattern as `TSDB_BIND`:

```yaml
# deploy/docker-compose.yml
ports:
  - "${GF_BIND:-127.0.0.1}:${GF_PORT:-3000}:3000"
```

## Usage

| Environment | `.env` setting | Effect |
|------|-------------|------|
| VPS (with Tailscale) | `GF_BIND=100.x.x.x` (Tailscale IP) | reachable only over the tailnet |
| Local development | unset (defaults to `127.0.0.1`) | reachable only from localhost |
| Public dashboard access (rare) | `GF_BIND=0.0.0.0` + a separate auth layer/reverse proxy | reachable on all interfaces, at your own risk |

## Alternatives considered (not adopted)

- **Keep `0.0.0.0`, rely on Grafana's own authentication**: the login page alone can't stop scanning/brute-forcing, and a leaked default password leaves zero protection — network-layer restriction is needed too.
- **No external access, SSH tunnel only**: secure enough for single-user use, but loses the flexibility of switching access scope per environment from the same compose file, and breaks consistency with `TSDB_BIND` — not adopted. `GF_BIND=0.0.0.0` stays available for the rare case of a genuinely public dashboard, at the operator's discretion with an added auth layer.
