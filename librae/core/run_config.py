"""Unified run configuration — single source of truth for all execution paths.

RunConfig is a frozen dataclass that holds all parameters for a run:
- Strategy params (stored in DB backtest_runs.params)
- Execution policy (typed fill and liquidity assumptions)
- Risk policy (typed engine-level portfolio limits)
- Perf params (stored in DB backtest_runs.perf_params, display only)
- Behavior params (not stored in DB)

CLI workflows use ``build_config()`` in ``orchestration/cli.py``; library
callers may construct the validated dataclass directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import asdict, dataclass, field
from functools import cached_property
from math import isfinite
from numbers import Real
from typing import Any, Literal

logger = logging.getLogger(__name__)

RunMode = Literal["backtest", "sim", "live"]
LiveMode = Literal["sim", "live"]


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """One isolated cash and PnL ledger.

    ``account_id`` is the key in ``RunConfig.accounts``. Currency conversion,
    transfers, borrowing, and netting across accounts are intentionally outside
    this contract.
    """

    currency: str
    initial_cash: float

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("account currency must be a non-empty string")
        if (
            isinstance(self.initial_cash, bool)
            or not isinstance(self.initial_cash, Real)
            or not isfinite(self.initial_cash)
            or self.initial_cash <= 0
        ):
            raise ValueError("account initial_cash must be finite and positive")


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Run-wide matching and pre-trade liquidity assumptions.

    ``default_fill_price`` is used by backtest and simulation when a strategy
    decision does not override ``fill_price``. Live market orders are filled
    by the broker and do not use this bar field.

    ``max_bar_volume_participation_rate`` caps the cumulative filled quantity for
    one symbol in one bar. ``None`` disables the cap. With a cap enabled,
    missing volume rejects the fill and insufficient volume produces a partial
    fill. The cap also applies to stops and forced exits.

    ``adv_lookback_sessions`` and ``max_adv_participation_rate`` form one
    optional session-level capacity limit. ADV uses exactly N completed
    sessions, excluding the execution session. Intraday data therefore needs
    a calendar_id for every configured symbol.

    ``live_order_timeout_seconds`` is a local live-trading safety timeout.
    After the first placement attempt, a non-terminal broker order older than
    this wall-clock duration is canceled and the deployment halts for operator
    review. It is not a broker time-in-force instruction. ``None`` leaves order
    lifetime to the broker.
    """

    default_fill_price: str = "open"
    max_bar_volume_participation_rate: float | None = 0.1
    adv_lookback_sessions: int | None = None
    max_adv_participation_rate: float | None = None
    live_order_timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.default_fill_price, str) or not self.default_fill_price:
            raise ValueError("default_fill_price must be a non-empty bar field name")
        for field_name in (
            "max_bar_volume_participation_rate",
            "max_adv_participation_rate",
        ):
            rate = getattr(self, field_name)
            if rate is not None and (
                isinstance(rate, bool)
                or not isinstance(rate, (int, float))
                or not isfinite(rate)
                or not 0 < rate <= 1
            ):
                raise ValueError(f"{field_name} must be in (0, 1] or None, got {rate}")

        lookback = self.adv_lookback_sessions
        if lookback is not None and (
            isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0
        ):
            raise ValueError(
                f"adv_lookback_sessions must be a positive integer or None, got {lookback}"
            )
        if (lookback is None) != (self.max_adv_participation_rate is None):
            raise ValueError(
                "adv_lookback_sessions and max_adv_participation_rate must be configured together"
            )
        timeout = self.live_order_timeout_seconds
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise ValueError(
                f"live_order_timeout_seconds must be a positive integer or None, got {timeout}"
            )


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Optional engine-level portfolio risk limits.

    Rate/weight limits are ratios, not percentages. ``max_order_notional`` is
    denominated in the account currency. ``None`` disables a limit.
    Strategy-specific parameters remain in ``RunConfig.params``.
    """

    max_position_weight: float | None = None
    max_drawdown_rate: float | None = None
    max_gross_exposure: float | None = None
    max_net_exposure: float | None = None
    max_order_notional: float | None = None
    max_limit_price_deviation_rate: float | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "max_position_weight",
            "max_drawdown_rate",
            "max_gross_exposure",
            "max_net_exposure",
            "max_order_notional",
            "max_limit_price_deviation_rate",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{field_name} must be finite and positive or None, got {value}")
        limit_price_rate = self.max_limit_price_deviation_rate
        if limit_price_rate is not None and limit_price_rate > 1:
            raise ValueError(
                f"max_limit_price_deviation_rate must be at most 1.0, got {limit_price_rate}"
            )


class FrozenDict(dict):
    """JSON-serializable dict that rejects mutation after construction."""

    def _immutable(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("RunConfig mappings are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze(value: Any) -> Any:
    """Recursively detach mutable caller-owned config values."""
    if isinstance(value, dict):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _sanitize_for_hash(obj: Any) -> Any:
    """Recursively normalize numeric types for deterministic config_hash.

    - float -> float.hex(): zero precision loss, e.g. (0.1).hex() -> '0x1.999999999999ap-4'
    - int -> unchanged, json.dumps outputs "1"
    - bool -> json.dumps outputs true/false (lowercase), already deterministic
    """
    if isinstance(obj, float):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _sanitize_for_hash(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_hash(v) for v in obj]
    return obj


def _mask_token(token: str) -> str:
    """Mask a token string: show first 4 + last 4 chars."""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def _get_code_rev() -> str:
    """Get short git rev + dirty flag for log_summary."""
    try:
        # WHY: single subprocess with combined command avoids spawning two processes.
        out = subprocess.check_output(
            ["git", "describe", "--always", "--dirty"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out or "unknown"
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class RunConfig:
    """Unified parameter container for all execution paths.

    CLI workflows create this through ``build_config()``. Library callers may
    construct it directly and receive the same validation.
    """

    # === Strategy identification (stored in DB) ===
    strategy_name: str
    symbols: tuple[str, ...]
    timeframe: str
    market: str
    data_source: str
    accounts: dict[str, AccountConfig]
    mode: RunMode
    execution: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    risk: RiskPolicy = field(default_factory=RiskPolicy)
    # Explicit live execution route. It is never inferred from market,
    # data_source, or symbol; instrument_overrides[symbol]["broker"] wins.
    broker: str | None = None
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] | None = None
    # Cost-model overrides. cost_overrides applies to every symbol in this
    # run (falls back to the built-in symbol/market registries for anything not listed);
    # symbol_cost_overrides applies to one symbol only and wins over
    # cost_overrides for that symbol — see CostModel.from_config(). This is
    # the escape hatch for a symbol that isn't in the built-in registry
    # (no file to edit, no path to point at — just pass
    # {"MYSYM": {"multiplier": 1.0}}) and for multi-asset runs mixing
    # symbols with different multipliers (e.g. TXFR1=200 + MXFR1=50 in the
    # same tw_futures run).
    cost_overrides: dict[str, float] | None = None
    symbol_cost_overrides: dict[str, dict[str, float]] | None = None
    # Broker/data routing metadata for one symbol. Cost fields remain in
    # symbol_cost_overrides so accounting inputs and venue identifiers cannot be
    # accidentally mixed into CostModel construction.
    instrument_overrides: dict[str, dict[str, str]] | None = None

    # === Perf params (stored in DB backtest_runs.perf_params, display only) ===
    annualize: bool = True
    risk_free_rate: float = 0.0
    periods_per_year: int = 365

    # === Operational behavior (excluded from config_hash) ===
    poll_seconds: int = 60
    reconciliation_interval_seconds: int = 300
    market_data_workers: int = 1
    no_db: bool = False
    dry_run: bool = False
    force: bool = False
    telegram_config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate invariants and detach mutable caller-owned values."""
        if not isinstance(self.execution, ExecutionPolicy):
            raise TypeError("execution must be an ExecutionPolicy")
        if not isinstance(self.risk, RiskPolicy):
            raise TypeError("risk must be a RiskPolicy")
        if isinstance(self.symbols, str):
            raise ValueError("symbols must be a collection of identifiers, not one string")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if not isinstance(self.accounts, dict) or not self.accounts:
            raise ValueError("accounts must be a non-empty mapping")
        normalized_accounts: dict[str, AccountConfig] = {}
        for account_id, account in self.accounts.items():
            if not isinstance(account_id, str) or not account_id:
                raise ValueError("account ids must be non-empty strings")
            if not isinstance(account, AccountConfig):
                raise TypeError("accounts values must be AccountConfig")
            normalized_accounts[account_id] = account
        object.__setattr__(self, "accounts", _freeze(normalized_accounts))
        for field_name in (
            "params",
            "cost_overrides",
            "symbol_cost_overrides",
            "instrument_overrides",
            "telegram_config",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _freeze(value))

        if not self.symbols or any(
            not isinstance(symbol, str) or not symbol for symbol in self.symbols
        ):
            raise ValueError("symbols must contain non-empty string identifiers")
        if self.mode not in ("backtest", "sim", "live"):
            raise ValueError(f"mode must be 'backtest', 'sim', or 'live', got {self.mode!r}")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("symbols must not contain duplicates")
        for field_name in ("strategy_name", "timeframe", "market", "data_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.broker is not None and (not isinstance(self.broker, str) or not self.broker):
            raise ValueError("broker must be a non-empty string or None")
        if (
            isinstance(self.risk_free_rate, bool)
            or not isinstance(self.risk_free_rate, Real)
            or not isfinite(self.risk_free_rate)
        ):
            raise ValueError("risk_free_rate must be a finite number")
        for field_name in ("annualize", "no_db", "dry_run", "force"):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a bool")
        if (
            isinstance(self.periods_per_year, bool)
            or not isinstance(self.periods_per_year, int)
            or self.periods_per_year <= 0
        ):
            raise ValueError("periods_per_year must be a positive integer")
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, int)
            or self.poll_seconds < 0
        ):
            raise ValueError("poll_seconds must be a non-negative integer")
        reconciliation_interval = self.reconciliation_interval_seconds
        if (
            isinstance(reconciliation_interval, bool)
            or not isinstance(reconciliation_interval, int)
            or reconciliation_interval <= 0
        ):
            raise ValueError("reconciliation_interval_seconds must be a positive integer")
        if (
            isinstance(self.market_data_workers, bool)
            or not isinstance(self.market_data_workers, int)
            or self.market_data_workers <= 0
        ):
            raise ValueError("market_data_workers must be a positive integer")
        if self.dry_run and not self.no_db:
            raise ValueError("dry_run=True requires no_db=True; use build_config()")
        legacy_execution_keys = {
            "fill_price",
            "max_volume_participation_pct",
            "max_volume_participation_rate",
            "max_bar_volume_participation_rate",
            "adv_lookback_sessions",
            "max_adv_participation_rate",
            "live_order_timeout_seconds",
        }
        invalid_keys = sorted(legacy_execution_keys & set(self.params or {}))
        if invalid_keys:
            raise ValueError(
                "execution settings no longer belong in params; move "
                f"{invalid_keys} to RunConfig.execution"
            )
        legacy_risk_keys = {
            "max_position_pct",
            "max_drawdown_pct",
            "max_gross_exposure_pct",
            "max_net_exposure_pct",
            "max_position_weight",
            "max_drawdown_rate",
            "max_gross_exposure",
            "max_net_exposure",
            "max_order_notional",
            "max_limit_price_deviation_rate",
        }
        invalid_keys = sorted(legacy_risk_keys & set(self.params or {}))
        if invalid_keys:
            raise ValueError(
                f"risk settings no longer belong in params; move {invalid_keys} to RunConfig.risk"
            )

    @property
    def symbol(self) -> str:
        """Primary symbol (single-asset convenience)."""
        return self.symbols[0]

    @cached_property
    def perf_params(self) -> dict[str, Any]:
        """Perf params dict, stored in DB backtest_runs.perf_params."""
        return {
            "annualize": self.annualize,
            "risk_free_rate": self.risk_free_rate,
            "periods_per_year": self.periods_per_year,
        }

    @cached_property
    def config_hash(self) -> str:
        """Deterministic hash of all result-affecting config.

        Includes: strategy_name, symbols, timeframe, market, data_source, broker,
        accounts, start, end, params, cost_overrides, symbol_cost_overrides,
        instrument_overrides, execution, risk.
        Excludes: perf params, behavior params.
        """
        blob = json.dumps(
            _sanitize_for_hash(
                {
                    "strategy_name": self.strategy_name,
                    # Primary-symbol order is observable engine behaviour.
                    "symbols": self.symbols,
                    "timeframe": self.timeframe,
                    "market": self.market,
                    "data_source": self.data_source,
                    "mode": self.mode,
                    "broker": self.broker,
                    "accounts": {
                        account_id: asdict(account) for account_id, account in self.accounts.items()
                    },
                    "start": self.start,
                    "end": self.end,
                    "params": self.params,
                    "cost_overrides": self.cost_overrides,
                    "symbol_cost_overrides": self.symbol_cost_overrides,
                    "instrument_overrides": self.instrument_overrides,
                    "execution": asdict(self.execution),
                    "risk": asdict(self.risk),
                }
            ),
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def log_summary(self) -> None:
        """Print all params at startup (strategy / perf / behavior, 3 sections)."""
        code_rev = _get_code_rev()
        tg = self.telegram_config or {}
        # Mask bot_token if present
        masked_tg = (
            {k: (_mask_token(str(v)) if "token" in k.lower() else v) for k, v in tg.items()}
            if tg
            else None
        )

        lines = [
            "=" * 60,
            "Run Config:",
            f"  strategy:    {self.strategy_name}",
            f"  symbols:     {self.symbols}",
            f"  timeframe:   {self.timeframe}",
            f"  mode:        {self.mode}",
            f"  data_source: {self.data_source}",
            f"  broker:      {self.broker}",
            f"  start:       {self.start}",
            f"  end:         {self.end}",
            f"  config_hash: {self.config_hash}",
            f"  code_rev:    {code_rev}",
            "  --- strategy params (stored in DB) ---",
            f"  accounts:    {dict(self.accounts)}",
            f"  params:      {self.params}",
            f"  cost_overrides: {self.cost_overrides}",
            f"  symbol_cost_overrides: {self.symbol_cost_overrides}",
            f"  instrument_overrides: {self.instrument_overrides}",
            f"  execution:   {self.execution}",
            f"  risk:        {self.risk}",
            "  --- perf params (stored in DB, display only) ---",
            f"  annualize:   {self.annualize}",
            f"  risk_free_rate: {self.risk_free_rate}",
            f"  periods_per_year: {self.periods_per_year}",
            "  --- operational behavior (excluded from config_hash) ---",
            f"  no_db:       {self.no_db}",
            f"  dry_run:     {self.dry_run}",
            f"  force:       {self.force}",
            f"  poll_seconds: {self.poll_seconds}",
            f"  reconcile:    {self.reconciliation_interval_seconds}",
            f"  data_workers: {self.market_data_workers}",
            f"  telegram:    {masked_tg}",
            "=" * 60,
        ]
        logger.info("\n".join(lines))
