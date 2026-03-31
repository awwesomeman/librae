"""Shared CLI utilities for strategy runners.

Provides base_parser() with common arguments shared across all strategies.
Strategy-specific run.py adds its own arguments on top.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml


def base_parser(description: str) -> argparse.ArgumentParser:
    """Build argument parser with common strategy runner arguments.

    Strategy runners call this, then add strategy-specific args before parse_args().
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--config", type=str, default=None,
                   help="path to strategy config YAML (values used as defaults)")
    p.add_argument("--mode", default="backtest", choices=["backtest", "sim", "live"])
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--market", default="crypto")
    p.add_argument("--months", type=int, default=6)
    p.add_argument("--initial-balance", type=float, default=100_000)
    p.add_argument("--poll-interval", type=int, default=60,
                   help="seconds between poll cycles (sim mode)")
    p.add_argument("--out-dir", default="data/backtests")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    p.add_argument("--no-annualize", action="store_true",
                   help="skip annualized metrics (backtest mode)")
    return p


def parse_with_config(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """Parse CLI args, applying config YAML as defaults if --config is provided.

    Priority: CLI explicit args > config YAML > parser defaults.
    """
    # WHY: Two-pass parse — first pass gets --config path,
    # second pass uses config values as defaults for unprovided args.
    args, _ = parser.parse_known_args()

    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            logging.getLogger(__name__).warning("Config file not found: %s", config_path)
        elif config_path.exists():
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            # Map YAML keys (underscore) to argparse dest names
            defaults = {k.replace("-", "_"): v for k, v in config.items()}
            parser.set_defaults(**defaults)

    return parser.parse_args()


def setup_logging() -> None:
    """Configure root logger for strategy runners."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
