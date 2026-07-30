"""Shared CLI utilities for strategy runners.

Provides:
- base_parser(): common CLI arguments
- parse_with_config(): merge config.yaml + CLI args
- build_config(): canonical CLI factory for RunConfig
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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import yaml
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "The YAML CLI requires the 'cli' extra: pip install 'librae[cli]'"
    ) from exc

from librae.core.run_config import (
    DEFAULT_POLL_SECONDS,
    AccountConfig,
    ExecutionPolicy,
    ReportingPolicy,
    RiskPolicy,
    RunConfig,
    RuntimePolicy,
)
from librae.core.utils import to_canonical

if TYPE_CHECKING:
    import pandas as pd
    from librae.core.strategy import Strategy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data-source -> annual trading days defaults
# ---------------------------------------------------------------------------
_DAILY_PERIODS_PER_YEAR: dict[str, int] = {
    "binance_spot": 365,
    "binance_futures_continuous": 365,
    "ibkr": 252,
    "shioaji": 252,
}


def base_parser(description: str) -> argparse.ArgumentParser:
    """Build argument parser with common strategy runner arguments.

    Strategy runners call this, then add strategy-specific args before parse_args().
    """
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help="path to strategy config YAML (overrides built-in config)",
    )
    p.add_argument("--mode", default="backtest", choices=["backtest", "sim", "live"])
    p.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        help="seconds between runtime poll cycles (required for sim/live; "
        "independent of the strategy bar timeframe)",
    )
    p.add_argument(
        "--reconciliation-interval-seconds",
        type=int,
        default=None,
        help="seconds between live broker/account reconciliation checks",
    )
    p.add_argument(
        "--market-data-workers",
        type=int,
        default=None,
        help="bounded concurrent market-data fetches (default: 1)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-db", action="store_true", help="skip writing to TimescaleDB")
    p.add_argument(
        "--no-annualize", action="store_true", help="skip annualized metrics (backtest mode)"
    )
    p.add_argument(
        "--force", action="store_true", help="skip config_hash cache, force fresh computation"
    )
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
    return datetime.fromtimestamp(floored_epoch, tz=UTC)


def _resolve_market_and_data_source(
    symbols: list[str],
    market: str | None,
    data_source: str | None,
    instrument_overrides: dict[str, dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Resolve routing metadata for every symbol.

    Registry values are authoritative; run-wide values are fallbacks for
    unregistered homogeneous universes, and instrument_overrides handles
    mixed universes. A heterogeneous result is represented as
    ``("multi", "multi")`` instead of borrowing the first symbol's route.
    """
    from librae.config.symbols import load_symbol_registry

    registry = load_symbol_registry()
    overrides = instrument_overrides or {}
    resolved_markets: set[str] = set()
    resolved_sources: set[str] = set()
    for sym in symbols:
        entry = registry.get(sym)
        route = overrides.get(sym, {})
        symbol_market = route.get("market") or (entry.market if entry else market)
        symbol_source = route.get("data_source") or (entry.data_source if entry else data_source)
        if symbol_market is None or symbol_source is None:
            raise ValueError(
                f"market/data_source cannot be resolved for {sym!r}; set run-wide "
                "values or instrument_overrides for that symbol"
            )
        if market not in (None, "multi") and market != symbol_market:
            raise ValueError(
                f"config.yaml market={market!r} for {sym!r} disagrees with "
                f"its resolved market ({symbol_market!r})"
            )
        if data_source not in (None, "multi") and data_source != symbol_source:
            raise ValueError(
                f"config.yaml data_source={data_source!r} for {sym!r} disagrees "
                f"with its resolved data source ({symbol_source!r})"
            )
        resolved_markets.add(symbol_market)
        resolved_sources.add(symbol_source)

    resolved_market = next(iter(resolved_markets)) if len(resolved_markets) == 1 else "multi"
    resolved_source = next(iter(resolved_sources)) if len(resolved_sources) == 1 else "multi"
    return resolved_market, resolved_source


# ---------------------------------------------------------------------------
# build_config() — CLI factory for RunConfig
# ---------------------------------------------------------------------------


def build_config(strategy_name: str, run_file: str) -> RunConfig:
    """Build RunConfig from CLI args + config.yaml.

    This is the canonical CLI factory for RunConfig. It:
    1. Parse start/end from params (or convert periods -> start/end)
    2. Extract per-run and per-symbol overrides
    3. Derive dry_run -> no_db
    4. Resolve an explicit return-observation annualization factor
    """
    p = base_parser(f"{strategy_name} strategy")
    config_path = Path(run_file).parent / "config.yaml"
    args = parse_with_config(p, config_path=config_path)

    scfg = getattr(args, "strategy", {})
    params = dict(scfg.get("params", {}))
    perf = scfg.get("perf", {})
    if not isinstance(perf, dict):
        raise ValueError("strategy.perf must be a mapping")
    unknown_perf_keys = set(perf) - {
        "annualize",
        "risk_free_rate",
        "periods_per_year",
    }
    if unknown_perf_keys:
        raise ValueError(f"unknown strategy.perf settings: {sorted(unknown_perf_keys)}")
    if "symbol_overrides" in scfg:
        raise ValueError("strategy.symbol_overrides was renamed to strategy.symbol_cost_overrides")
    execution_raw = scfg.get("execution", {})
    if not isinstance(execution_raw, dict):
        raise ValueError("strategy.execution must be a mapping")
    unknown_execution_keys = set(execution_raw) - {
        "default_fill_price",
        "max_bar_volume_participation_rate",
        "adv_lookback_sessions",
        "max_adv_participation_rate",
        "live_order_timeout_seconds",
        "warmup_periods",
    }
    if unknown_execution_keys:
        raise ValueError(f"unknown strategy.execution settings: {sorted(unknown_execution_keys)}")
    risk_raw = scfg.get("risk", {})
    if not isinstance(risk_raw, dict):
        raise ValueError("strategy.risk must be a mapping")
    unknown_risk_keys = set(risk_raw) - {
        "max_position_weight",
        "max_drawdown_rate",
        "max_gross_exposure",
        "max_net_exposure",
        "max_order_notional",
        "max_limit_price_deviation_rate",
    }
    if unknown_risk_keys:
        raise ValueError(f"unknown strategy.risk settings: {sorted(unknown_risk_keys)}")
    symbols_raw = scfg.get("symbol", scfg.get("symbols", ""))
    if isinstance(symbols_raw, list):
        symbols = symbols_raw
    else:
        symbols = [s.strip() for s in str(symbols_raw).split(",")]

    timeframe = scfg.get("timeframe", "H1")
    instrument_overrides = scfg.get("instrument_overrides")
    market, data_source = _resolve_market_and_data_source(
        symbols,
        scfg.get("market"),
        scfg.get("data_source"),
        instrument_overrides,
    )
    if "initial_balance" in scfg:
        raise ValueError(
            "strategy.initial_balance was replaced by strategy.accounts; "
            "configure {account_id: {currency, initial_cash}}"
        )
    accounts_raw = scfg.get("accounts")
    accounts: dict[str, AccountConfig] = {}
    if accounts_raw is None:
        from librae.config.symbols import load_symbol_registry

        registry = load_symbol_registry()
        currencies = {
            (instrument_overrides or {}).get(symbol, {}).get("currency")
            or (registry[symbol].currency if symbol in registry else None)
            for symbol in symbols
        }
        if None in currencies or len(currencies) != 1:
            raise ValueError(
                "strategy.accounts is required when account currency cannot be "
                "inferred as one value for all symbols"
            )
        accounts["default"] = AccountConfig(
            currency=str(next(iter(currencies))),
            initial_cash=scfg.get("initial_cash", 100_000),
        )
    else:
        if not isinstance(accounts_raw, dict) or not accounts_raw:
            raise ValueError(
                "strategy.accounts must be a non-empty mapping of "
                "account_id to {currency, initial_cash}"
            )
        if "initial_cash" in scfg:
            raise ValueError("strategy.initial_cash is only valid for an inferred single account")
        for account_id, raw_account in accounts_raw.items():
            if not isinstance(raw_account, dict):
                raise ValueError(f"strategy.accounts.{account_id} must be a mapping")
            unknown_account_keys = set(raw_account) - {"currency", "initial_cash"}
            if unknown_account_keys:
                raise ValueError(
                    f"unknown strategy.accounts.{account_id} settings: "
                    f"{sorted(unknown_account_keys)}"
                )
            try:
                accounts[str(account_id)] = AccountConfig(
                    currency=raw_account["currency"],
                    initial_cash=raw_account["initial_cash"],
                )
            except KeyError as exc:
                raise ValueError(
                    f"strategy.accounts.{account_id} requires currency and initial_cash"
                ) from exc

    # 1. Parse start/end (pop from params so they don't enter config_hash via params)
    start = params.pop("start", None)
    end = params.pop("end", None)
    if start is None and "periods" in params:
        from librae.core.utils import interval_to_timedelta

        periods = params.pop("periods")
        now = datetime.now(UTC)
        end_dt = floor_to_timeframe(now, timeframe)
        start_dt = end_dt - interval_to_timedelta(timeframe) * periods
        start = start_dt.isoformat()
        end = end_dt.isoformat()
    else:
        # WHY: periods is a data-window specifier, not a strategy param.
        # Pop it even when start/end are provided, to keep params clean.
        params.pop("periods", None)

    # 2. Extract per-run and per-symbol configuration
    cost_overrides = scfg.get("cost_overrides")
    symbol_cost_overrides = scfg.get("symbol_cost_overrides")

    # 3. dry_run -> no_db
    dry_run = args.dry_run
    no_db = args.no_db or dry_run

    # poll_seconds has no implicit default in sim/live: it is an API polling
    # cadence, not the strategy's bar timeframe, and must be chosen explicitly.
    if args.mode in ("sim", "live") and args.poll_seconds is None:
        raise ValueError(
            "--poll-seconds is required for sim/live mode. "
            f"Set it explicitly for timeframe={timeframe!r}; it should normally "
            "be no slower than one completed bar."
        )
    poll_seconds = args.poll_seconds if args.poll_seconds is not None else DEFAULT_POLL_SECONDS
    reconciliation_interval_seconds = (
        args.reconciliation_interval_seconds
        if args.reconciliation_interval_seconds is not None
        else 300
    )
    market_data_workers = args.market_data_workers if args.market_data_workers is not None else 1

    execution = ExecutionPolicy(**execution_raw)
    risk = RiskPolicy(**risk_raw)

    # 4. Perf params (with known exchange-calendar defaults)
    configured_periods_per_year = perf.get("periods_per_year")
    if args.no_annualize:
        annualize = False
    elif perf.get("annualize") is not None:
        annualize = perf["annualize"]
    else:
        annualize = True
    default_periods_per_year = (
        _DAILY_PERIODS_PER_YEAR.get(data_source) if to_canonical(timeframe) == "D1" else None
    )
    if annualize and configured_periods_per_year is None and default_periods_per_year is None:
        raise ValueError(
            "strategy.perf.periods_per_year is required when annualizing "
            f"timeframe={timeframe!r}, data_source={data_source!r}; "
            "set the number of return observations per year explicitly"
        )
    periods_per_year = (
        configured_periods_per_year
        if configured_periods_per_year is not None
        else default_periods_per_year or 365
    )

    return RunConfig(
        strategy_name=strategy_name,
        symbols=symbols,
        timeframe=timeframe,
        market=market,
        data_source=data_source,
        accounts=accounts,
        mode=args.mode,
        execution=execution,
        risk=risk,
        broker=scfg.get("broker"),
        start=start,
        end=end,
        params=params or None,
        cost_overrides=cost_overrides,
        symbol_cost_overrides=symbol_cost_overrides,
        instrument_overrides=instrument_overrides,
        reporting=ReportingPolicy(
            annualize=annualize,
            risk_free_rate=float(perf.get("risk_free_rate", 0.0)),
            periods_per_year=periods_per_year,
        ),
        runtime=RuntimePolicy(
            poll_seconds=poll_seconds,
            reconciliation_interval_seconds=reconciliation_interval_seconds,
            market_data_workers=market_data_workers,
        ),
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
    def wrapper(config: RunConfig) -> None:
        if not config.no_db and not config.force:
            existing = check_existing_run(config)
            if existing:
                return
        fn(config)

    return wrapper


def check_existing_run(config: RunConfig) -> str | None:
    """config_hash dedup check. Returns existing run_id or None.

    Shared by strategy backtest + signal backtest.
    """
    try:
        from db.timescale_reader import get_run_by_config_hash
    except ImportError as exc:
        logger.warning(
            "Database dedup is unavailable because the optional 'db' dependencies "
            "could not be imported (%s). From a repository clone run "
            "'uv sync --extra db', or pass --no-db when persistence is not needed.",
            exc,
        )
        return None

    try:
        existing = get_run_by_config_hash(config.config_hash)
    except Exception:
        logger.warning(
            "config_hash dedup check failed (TimescaleDB unreachable?) — "
            "skipping dedup and running the backtest. Pass --no-db to "
            "suppress this check entirely.",
            exc_info=True,
        )
        return None
    if not existing:
        return None

    if existing["perf_params"] != config.perf_params:
        # Lightweight path: skip backtest, only recompute metrics
        try:
            from db.timescale_writer import _update_perf_params, refresh_performance

            for account_id in config.accounts:
                refresh_performance(
                    existing["run_id"],
                    account_id=account_id,
                    config=config,
                )
            _update_perf_params(existing["run_id"], config.perf_params)
            logger.info(
                "Recomputed metrics for run_id=%s (perf_params changed)", existing["run_id"]
            )
        except Exception:
            logger.exception("Failed to recompute metrics, will run full backtest")
            return None
    else:
        logger.info(
            "Run with config_hash=%s exists (run_id=%s), skipping",
            config.config_hash,
            existing["run_id"],
        )
    return existing["run_id"]


# ---------------------------------------------------------------------------
# run_dispatch() — shared main() for all strategy runners
# ---------------------------------------------------------------------------


def run_dispatch(
    strategy_name: str,
    run_file: str,
    run_backtest: Callable[[RunConfig], None],
    run_realtime: Callable[[RunConfig], None] | None = None,
) -> None:
    """Run a strategy through its declared mode handlers.

    A runner that omits ``run_realtime`` is explicitly backtest-only. This
    fails before strategy execution with an actionable mode error instead of
    relying on a placeholder callback to raise ``NotImplementedError``.
    """
    setup_logging()
    config = build_config(strategy_name, run_file)
    if config.mode == "backtest":
        config.log_summary()
        with_dedup_check(run_backtest)(config)
        return
    if run_realtime is None:
        raise ValueError(
            f"{strategy_name!r} does not support mode={config.mode!r}; "
            "supported modes: ['backtest']. Use --mode backtest, or provide "
            "a real-time runner with market-data, broker, and state wiring."
        )
    config.log_summary()
    run_realtime(config)


# ---------------------------------------------------------------------------
# run_realtime_generic() — the sim/live half of a strategy's run.py.
# The backtest half (fetch -> prepare_signals -> MultiIndex -> Backtest ->
# save) depends on a data-access layer that lives outside librae, so it
# doesn't belong here — this one doesn't, since LiveTrader takes config + a
# feature_fn directly and does its own fetching via config.market.
# ---------------------------------------------------------------------------


def run_realtime_generic(
    config: RunConfig,
    strategy: Strategy,
    prepare_signals: Callable[[pd.DataFrame], pd.DataFrame],
) -> None:
    """Run the repository's default sim/live deployment wiring."""
    from orchestration.live import build_live_trader

    trader = build_live_trader(strategy, prepare_signals, config=config)
    trader.run()


def setup_logging() -> None:
    """Configure root logger for strategy runners."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
