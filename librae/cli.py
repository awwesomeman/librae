"""Shared CLI utilities for strategy runners.

Provides:
- base_parser(): common CLI arguments
- parse_with_config(): merge config.yaml + CLI args
- build_config(): sole factory for RunConfig
- run_dispatch(): shared main() for all strategy runners
- with_dedup_check(): config_hash dedup wrapper for backtest
- setup_logging(): root logger config

Config merge priority (low -> high):
    strategy config.yaml -> CLI args
Dict-valued YAML keys (e.g. telegram, strategy) bypass argparse -> attached as dict on Namespace.
"""
from __future__ import annotations

import argparse
import functools
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from librae.core.run_config import RunConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data-source -> annual trading days defaults
# ---------------------------------------------------------------------------
_DATA_SOURCE_ANNUAL_PERIODS: dict[str, int] = {
    "binance_spot": 365,
    "binance_futures": 365,
    "tw_futures": 252,
    "tw_equity": 252,
}


def base_parser(description: str) -> argparse.ArgumentParser:
    """Build argument parser with common strategy runner arguments.

    Strategy runners call this, then add strategy-specific args before parse_args().
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--config", type=str, default=None,
                   help="path to strategy config YAML (overrides built-in config)")
    p.add_argument("--mode", default="backtest", choices=["backtest", "sim", "live"])
    p.add_argument("--poll-seconds", type=int, default=60,
                   help="seconds between poll cycles (sim mode)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    p.add_argument("--no-annualize", action="store_true",
                   help="skip annualized metrics (backtest mode)")
    p.add_argument("--force", action="store_true",
                   help="skip config_hash cache, force fresh computation")
    return p


def parse_with_config(
    parser: argparse.ArgumentParser,
    config_path: str | Path | None = None,
) -> argparse.Namespace:
    """Parse CLI args, applying config YAML as defaults.

    Args:
        config_path: Path to strategy's own config.yaml -- the single source
            of truth for strategy parameters. ``--config`` on the CLI
            overrides this path.

    Priority (low -> high): config_path -> --config (if provided) -> CLI args.
    Dict-valued YAML keys are attached to the returned Namespace as dict
    attributes, not passed through argparse.
    """
    # WHY: Two-pass parse -- first pass gets --config path,
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
            # that argparse can't handle -- attach them to Namespace directly.
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


# ---------------------------------------------------------------------------
# Timeframe flooring (for periods -> start/end conversion)
# ---------------------------------------------------------------------------


def floor_to_timeframe(dt: datetime, timeframe: str) -> datetime:
    """Floor datetime to timeframe boundary.

    E.g. 14:32:01 H1 -> 14:00:00, 14:32:01 M5 -> 14:30:00
    Ensures config_hash stability within the same bar window.
    """
    from librae.core.utils import interval_to_timedelta

    td = interval_to_timedelta(timeframe)
    total_secs = int(td.total_seconds())
    if total_secs <= 0:
        return dt
    epoch = int(dt.timestamp())
    floored_epoch = (epoch // total_secs) * total_secs
    return datetime.fromtimestamp(floored_epoch, tz=timezone.utc)


# ---------------------------------------------------------------------------
# build_config() — sole factory for RunConfig
# ---------------------------------------------------------------------------


def build_config(strategy_name: str, run_file: str) -> RunConfig:
    """Build RunConfig from CLI args + config.yaml.

    This is the sole factory for RunConfig. Handles:
    1. Parse start/end from params (or convert periods -> start/end)
    2. Extract cost_overrides from strategy section
    3. Derive dry_run -> no_db
    4. Derive annual_periods from data_source if not specified in perf
    """
    p = base_parser(f"{strategy_name} strategy")
    config_path = Path(run_file).parent / "config.yaml"
    args = parse_with_config(p, config_path=config_path)

    scfg = getattr(args, "strategy", {})
    params = dict(scfg.get("params", {}))
    perf = scfg.get("perf", {})
    symbols_raw = scfg.get("symbol", scfg.get("symbols", ""))
    if isinstance(symbols_raw, list):
        symbols = symbols_raw
    else:
        symbols = [s.strip() for s in str(symbols_raw).split(",")]

    timeframe = scfg.get("timeframe", "H1")
    market = scfg.get("market", "crypto")
    data_source = scfg.get("data_source", "binance_spot")
    initial_balance = float(scfg.get("initial_balance", 100_000))

    # 1. Parse start/end (pop from params so they don't enter config_hash via params)
    start = params.pop("start", None)
    end = params.pop("end", None)
    if start is None and "periods" in params:
        from librae.core.utils import interval_to_timedelta
        periods = params.pop("periods")
        now = datetime.now(timezone.utc)
        end_dt = floor_to_timeframe(now, timeframe)
        start_dt = end_dt - interval_to_timedelta(timeframe) * periods
        start = start_dt.isoformat()
        end = end_dt.isoformat()
    else:
        # WHY: periods is a data-window specifier, not a strategy param.
        # Pop it even when start/end are provided, to keep params clean.
        params.pop("periods", None)

    # 2. Extract cost_overrides
    cost_overrides = scfg.get("cost_overrides")

    # 3. dry_run -> no_db
    dry_run = args.dry_run
    no_db = args.no_db or dry_run

    # 4. Perf params (with data_source defaults)
    default_annual = _DATA_SOURCE_ANNUAL_PERIODS.get(data_source, 365)
    if args.no_annualize:
        annualize = False
    elif perf.get("annualize") is not None:
        annualize = perf["annualize"]
    else:
        annualize = True

    return RunConfig(
        strategy_name=strategy_name,
        symbols=symbols,
        timeframe=timeframe,
        market=market,
        data_source=data_source,
        initial_balance=initial_balance,
        mode=args.mode,
        start=start,
        end=end,
        params=params or None,
        cost_overrides=cost_overrides,
        annualize=annualize,
        risk_free_rate=float(perf.get("risk_free_rate", 0.0)),
        annual_periods=int(perf.get("annual_periods", default_annual)),
        ddof=int(perf.get("ddof", 1)),
        poll_seconds=args.poll_seconds,
        no_db=no_db,
        dry_run=dry_run,
        force=args.force,
        telegram_config=getattr(args, "telegram", None),
    )


# ---------------------------------------------------------------------------
# with_dedup_check() — config_hash dedup for backtest
# ---------------------------------------------------------------------------


def with_dedup_check(fn: Callable[[RunConfig], None]) -> Callable[[RunConfig], None]:
    """Wrap run_backtest to check config_hash before running.

    Only for backtest mode. Sim/live paths don't use this.
    """
    @functools.wraps(fn)
    def wrapper(cfg: RunConfig) -> None:
        if not cfg.no_db and not cfg.force:
            existing = check_existing_run(cfg)
            if existing:
                return
        fn(cfg)
    return wrapper


def check_existing_run(cfg: RunConfig) -> str | None:
    """config_hash dedup check. Returns existing run_id or None.

    Shared by strategy backtest + signal backtest.
    """
    try:
        from db.timescale_reader import find_run_by_config_hash
    except ImportError:
        return None

    existing = find_run_by_config_hash(cfg.config_hash)
    if not existing:
        return None

    if existing["perf_params"] != cfg.perf_params:
        # Lightweight path: skip backtest, only recompute metrics
        try:
            from db.timescale_writer import _update_perf_params, refresh_performance
            refresh_performance(existing["run_id"], cfg=cfg)
            _update_perf_params(existing["run_id"], cfg.perf_params)
            logger.info("Recomputed metrics for run_id=%s (perf_params changed)",
                        existing["run_id"])
        except Exception:
            logger.exception("Failed to recompute metrics, will run full backtest")
            return None
    else:
        logger.info("Run with config_hash=%s exists (run_id=%s), skipping",
                     cfg.config_hash, existing["run_id"])
    return existing["run_id"]


# ---------------------------------------------------------------------------
# run_dispatch() — shared main() for all strategy runners
# ---------------------------------------------------------------------------


def run_dispatch(
    strategy_name: str,
    run_file: str,
    run_backtest: Callable[[RunConfig], None],
    run_realtime: Callable[[RunConfig], None],
) -> None:
    """Shared main() for all strategy runners."""
    setup_logging()
    cfg = build_config(strategy_name, run_file)
    cfg.log_summary()
    dispatch: dict[str, Callable[[RunConfig], None]] = {
        "backtest": with_dedup_check(run_backtest),
        "sim": run_realtime,
        "live": run_realtime,
    }
    dispatch[cfg.mode](cfg)


def setup_logging() -> None:
    """Configure root logger for strategy runners."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
