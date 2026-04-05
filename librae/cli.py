"""Shared CLI utilities for strategy runners.

Provides base_parser() with common arguments shared across all strategies.
Strategy-specific run.py adds its own arguments on top.

Config merge priority (low → high):
    strategy config.yaml → CLI args
Dict-valued YAML keys (e.g. telegram, strategy) bypass argparse → attached as dict on Namespace.
"""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def base_parser(description: str) -> argparse.ArgumentParser:
    """Build argument parser with common strategy runner arguments.

    Strategy runners call this, then add strategy-specific args before parse_args().
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--config", type=str, default=None,
                   help="path to strategy config YAML (overrides built-in config)")
    # Runtime flags only — things that genuinely change per-run.
    # Everything else (symbol, market, poll_seconds, etc.) lives in config.yaml.
    p.add_argument("--mode", default="backtest", choices=["backtest", "sim", "live"])
    p.add_argument("--poll-seconds", type=int, default=60,
                   help="seconds between poll cycles (sim mode)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    p.add_argument("--no-annualize", action="store_true",
                   help="skip annualized metrics (backtest mode)")
    return p


def parse_with_config(
    parser: argparse.ArgumentParser,
    config_path: str | Path | None = None,
) -> argparse.Namespace:
    """Parse CLI args, applying config YAML as defaults.

    Args:
        config_path: Path to strategy's own config.yaml — the single source
            of truth for strategy parameters. ``--config`` on the CLI
            overrides this path.

    Priority (low → high): config_path → --config (if provided) → CLI args.
    Dict-valued YAML keys are attached to the returned Namespace as dict
    attributes, not passed through argparse.
    """
    # WHY: Two-pass parse — first pass gets --config path,
    # second pass uses config values as defaults for unprovided args.
    args, _ = parser.parse_known_args()

    resolved = Path(args.config) if args.config else (Path(config_path) if config_path else None)

    structured: dict[str, object] = {}

    if resolved:
        try:
            with open(resolved) as f:
                config = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.warning("Config file not found: %s", resolved)
        else:
            # WHY: dict values are structured config (strategy, telegram, etc.)
            # that argparse can't handle — attach them to Namespace directly.
            # Scalars go to argparse as defaults so CLI args can override them.
            flat: dict[str, object] = {}
            for k, v in config.items():
                if isinstance(v, dict):
                    structured[k] = v
                else:
                    flat[k.replace("-", "_")] = v

            parser.set_defaults(**flat)

    result = parser.parse_args()

    # Attach structured keys as attributes
    for k, v in structured.items():
        setattr(result, k, v)

    return result


def setup_logging() -> None:
    """Configure root logger for strategy runners."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
