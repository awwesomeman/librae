#!/usr/bin/env python3
"""Dev tool — push dashboard JSON to Grafana via HTTP API for instant preview.

Writes to Grafana's internal DB (not provisioning files).
Changes are overwritten on Grafana restart by provisioning.
Source of truth: app/grafana/provisioning/dashboards/json/*.json

Usage:
    python deploy/dev_push_dashboard.py
    python deploy/dev_push_dashboard.py --grafana-url http://host:3000 --grafana-user admin --grafana-password secret
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import re
import subprocess
import sys

import requests

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Setup Grafana datasource uid and deploy dashboards")
    p.add_argument("--grafana-url", default="http://localhost:3000", help="Grafana base URL")
    p.add_argument("--grafana-user", default="admin", help="Grafana admin username")
    p.add_argument("--grafana-password", default="admin", help="Grafana admin password")
    return p.parse_args()


def get_timescaledb_uid(base_url: str, auth: tuple[str, str]) -> tuple[str | None, str | None]:
    """Query Grafana API for TimescaleDB/PostgreSQL datasource."""
    r = requests.get(f"{base_url}/api/datasources", auth=auth, timeout=10)
    r.raise_for_status()
    for ds in r.json():
        name = ds.get("name", "").lower()
        ds_type = ds.get("type", "").lower()
        if "timescale" in name or "postgres" in ds_type:
            return ds["uid"], ds["type"]
    return None, None


def update_generate_dashboards(uid: str, ds_type: str) -> None:
    """Update DATASOURCE dict in app/grafana/generate_dashboards.py."""
    path = "app/grafana/generate_dashboards.py"
    with open(path) as f:
        content = f.read()
    new_ds = json.dumps({"type": ds_type, "uid": uid})
    updated = re.sub(r'DATASOURCE\s*:\s*dict\s*=\s*\{[^}]*\}', f'DATASOURCE: dict = {new_ds}', content)
    if updated == content:
        if not re.search(r'DATASOURCE\s*:\s*dict\s*=\s*\{[^}]*\}', content):
            logger.warning("DATASOURCE pattern not found in %s", path)
        else:
            logger.info("DATASOURCE already up-to-date in %s", path)
        return
    with open(path, "w") as f:
        f.write(updated)
    logger.info("Updated DATASOURCE uid=%s type=%s", uid, ds_type)


def delete_old_dashboards(base_url: str, auth: tuple[str, str]) -> None:
    """Remove legacy per-mode dashboards from Grafana."""
    old_uids = ["backtest_dashboard", "sim_dashboard", "live_dashboard"]
    for uid in old_uids:
        r = requests.delete(f"{base_url}/api/dashboards/uid/{uid}", auth=auth, timeout=10)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        logger.info("Deleted old dashboard: %s", uid)


def deploy_dashboards(base_url: str, auth: tuple[str, str]) -> None:
    """Re-generate dashboard JSON and deploy to Grafana."""
    subprocess.run([sys.executable, "app/grafana/generate_dashboards.py"], check=True)
    dashboard_dir = "app/grafana/provisioning/dashboards/json"
    for fpath in sorted(pathlib.Path(dashboard_dir).glob("*.json")):
        with open(fpath) as f:
            d = json.load(f)
        d.pop("id", None)
        r = requests.post(
            f"{base_url}/api/dashboards/db",
            json={"dashboard": d, "folderId": 0, "overwrite": True},
            auth=auth,
            timeout=30,
        )
        r.raise_for_status()
        logger.info("%s: %s", fpath.name, r.json().get("status", "?"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = parse_args()
    auth = (args.grafana_user, args.grafana_password)

    uid, ds_type = get_timescaledb_uid(args.grafana_url, auth)
    if not uid:
        logger.error("TimescaleDB datasource not found in Grafana")
        sys.exit(1)

    update_generate_dashboards(uid, ds_type)
    delete_old_dashboards(args.grafana_url, auth)
    deploy_dashboards(args.grafana_url, auth)
    logger.info("Grafana setup complete")


if __name__ == "__main__":
    main()
