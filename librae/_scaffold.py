"""``librae init`` — scaffold a starter .env.example for pip-installed usage.

Cloning this repo already gets you .env.example/.env.secrets.example at the
repo root. This command is for the other workflow: `pip install librae` with
no clone, where there's no repo tree to copy a template from — so the
template ships as package data instead (see librae/_scaffold/env.example).
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

    args = parser.parse_args()

    if args.command == "init":
        _init(force=args.force)


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
