"""The ``librae`` command: ``init`` scaffolds an env template, ``doctor`` checks one.

``init`` is for the `pip install librae` workflow with no clone, where there's
no repo tree to copy a template from — so the template ships as package data
(see librae/_scaffold/env.example). ``doctor`` validates the .env and
.env.secrets in the current directory against librae.config.env's registry.
"""

from __future__ import annotations

import argparse
import shutil
from importlib.resources import files
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="librae", description="librae project scaffolding")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="write a starter .env.example")
    init_parser.add_argument(
        "--force", action="store_true", help="overwrite .env.example if it already exists"
    )
    subparsers.add_parser(
        "doctor",
        help="check ./.env and ./.env.secrets: misspelled names, secrets in the synced file, "
        "half-configured key pairs, DSN role and password (with a single .env and no "
        ".env.secrets, the file-placement checks are skipped)",
    )
    db_parser = subparsers.add_parser("db", help="inspect or migrate the reference database schema")
    db_parser.add_argument("action", choices=("preflight", "migrate"))

    args = parser.parse_args()

    if args.command == "init":
        _init(force=args.force)
    elif args.command == "doctor":
        raise SystemExit(_doctor())
    elif args.command == "db":
        from librae.db.schema import _run_cli

        raise SystemExit(_run_cli(args.action))


def _doctor() -> int:
    from librae.config.env import doctor

    findings = doctor(Path.cwd())
    for finding in findings:
        print(f"{finding.level}: {finding.message}")
    errors = sum(finding.level == "error" for finding in findings)
    print("ok" if not findings else f"{errors} error(s), {len(findings) - errors} warning(s)")
    return 1 if errors else 0


def _init(*, force: bool) -> None:
    dst = Path(".env.example")
    if dst.exists() and not force:
        print(f"{dst} already exists, use --force to overwrite")
        return
    src = files("librae") / "_scaffold" / "env.example"
    shutil.copy(str(src), dst)
    print(f"Wrote {dst} — copy to .env and fill in the values you need")


if __name__ == "__main__":
    main()
