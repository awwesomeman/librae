#!/usr/bin/env python3
"""Grafana setup script — auto-detect datasource uid, update generator, deploy dashboards.

Usage:
    python scripts/setup_grafana.py
    python scripts/setup_grafana.py --grafana-url http://host:3000 --grafana-user admin --grafana-password secret
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import requests


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
    content = open(path).read()
    new_ds = f'{{"type": "{ds_type}", "uid": "{uid}"}}'
    updated = re.sub(r'DATASOURCE = \{[^}]*\}', f'DATASOURCE = {new_ds}', content)
    if updated == content:
        print(f"WARNING: DATASOURCE pattern not found in {path}, no changes made")
        return
    open(path, "w").write(updated)
    print(f"Updated DATASOURCE uid={uid} type={ds_type}")


def deploy_dashboards(base_url: str, auth: tuple[str, str]) -> None:
    """Re-generate dashboard JSONs and deploy to Grafana."""
    subprocess.run([sys.executable, "app/grafana/generate_dashboards.py"], check=True)
    for fname in ["backtest_dashboard.json", "sim_dashboard.json", "live_dashboard.json"]:
        fpath = f"app/grafana/dashboards/{fname}"
        d = json.load(open(fpath))
        d.pop("id", None)
        r = requests.post(
            f"{base_url}/api/dashboards/db",
            json={"dashboard": d, "folderId": 0, "overwrite": True},
            auth=auth,
            timeout=30,
        )
        r.raise_for_status()
        print(f"  {fname}: {r.json().get('status', '?')}")


def main() -> None:
    args = parse_args()
    auth = (args.grafana_user, args.grafana_password)

    uid, ds_type = get_timescaledb_uid(args.grafana_url, auth)
    if not uid:
        print("ERROR: TimescaleDB datasource not found in Grafana")
        sys.exit(1)

    update_generate_dashboards(uid, ds_type)
    deploy_dashboards(args.grafana_url, auth)
    print("✅ Grafana setup complete")


if __name__ == "__main__":
    main()
