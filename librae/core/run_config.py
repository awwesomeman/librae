"""Unified run configuration — single source of truth for all execution paths.

RunConfig is a frozen dataclass that holds result-affecting run parameters and
one explicit non-result policy:
- Strategy params (stored in DB backtest_runs.params)
- Execution policy (typed fill and liquidity assumptions)
- Risk policy (typed engine-level portfolio limits)
- Runtime polling policy (not stored in DB)

CLI workflows use ``build_run()`` in ``librae/orchestration/cli.py``; library
callers may construct the validated dataclass directly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from functools import cached_property
from math import isfinite
from numbers import Real
from typing import Any, Literal

RunMode = Literal["backtest", "sim", "live"]
LiveMode = Literal["sim", "live"]
DEFAULT_POLL_SECONDS = 60


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """The single cash and PnL ledger used by one engine run.

    A run owns exactly one account; callers coordinate multiple accounts as
    separate runs because Librae does not provide FX, transfers, settlement,
    or cross-account netting.
    """

    currency: str
    initial_cash: float
    account_id: str = "default"

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id:
            raise ValueError("account_id must be a non-empty string")
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

    ``warmup_periods`` is the retained live/sim history used by feature
    calculation. It is explicit and validated here because too short a window
    changes engine behavior and can invalidate ADV or strategy inputs.
    """

    default_fill_price: str = "open"
    max_bar_volume_participation_rate: float | None = 0.1
    adv_lookback_sessions: int | None = None
    max_adv_participation_rate: float | None = None
    live_order_timeout_seconds: int | None = None
    warmup_periods: int = 720

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
        if (
            isinstance(self.warmup_periods, bool)
            or not isinstance(self.warmup_periods, int)
            or self.warmup_periods <= 0
        ):
            raise ValueError(
                f"warmup_periods must be a positive integer, got {self.warmup_periods}"
            )


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """Operational cadence and concurrency for sim/live polling."""

    poll_seconds: int = DEFAULT_POLL_SECONDS
    reconciliation_interval_seconds: int = 300
    market_data_workers: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.poll_seconds, bool)
            or not isinstance(self.poll_seconds, int)
            or self.poll_seconds < 0
        ):
            raise ValueError("poll_seconds must be a non-negative integer")
        if (
            isinstance(self.reconciliation_interval_seconds, bool)
            or not isinstance(self.reconciliation_interval_seconds, int)
            or self.reconciliation_interval_seconds <= 0
        ):
            raise ValueError("reconciliation_interval_seconds must be a positive integer")
        if (
            isinstance(self.market_data_workers, bool)
            or not isinstance(self.market_data_workers, int)
            or self.market_data_workers <= 0
        ):
            raise ValueError("market_data_workers must be a positive integer")


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


@dataclass(frozen=True)
class RunConfig:
    """Unified parameter container for all execution paths.

    CLI workflows create this through ``build_run()``. Library callers may
    construct it directly and receive the same validation.
    """

    # === Strategy identification (stored in DB) ===
    strategy_name: str
    symbols: tuple[str, ...]
    timeframe: str
    market: str
    data_source: str
    account: AccountConfig
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
    instrument_overrides: dict[str, dict[str, object]] | None = None

    # === Non-result policies (excluded from config_hash) ===
    runtime: RuntimePolicy = field(default_factory=RuntimePolicy)

    def __post_init__(self) -> None:
        """Validate invariants and detach mutable caller-owned values."""
        if not isinstance(self.execution, ExecutionPolicy):
            raise TypeError("execution must be an ExecutionPolicy")
        if not isinstance(self.risk, RiskPolicy):
            raise TypeError("risk must be a RiskPolicy")
        if not isinstance(self.runtime, RuntimePolicy):
            raise TypeError("runtime must be a RuntimePolicy")
        if isinstance(self.symbols, str):
            raise ValueError("symbols must be a collection of identifiers, not one string")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        if not isinstance(self.account, AccountConfig):
            raise TypeError("account must be an AccountConfig")
        for field_name in (
            "params",
            "cost_overrides",
            "symbol_cost_overrides",
            "instrument_overrides",
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
        legacy_execution_keys = {
            "fill_price",
            "max_volume_participation_pct",
            "max_volume_participation_rate",
            "max_bar_volume_participation_rate",
            "adv_lookback_sessions",
            "max_adv_participation_rate",
            "live_order_timeout_seconds",
            "warmup_periods",
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

    @property
    def account_id(self) -> str:
        """Stable identity of this run's single account."""
        return self.account.account_id

    @cached_property
    def config_hash(self) -> str:
        """Deterministic hash of all result-affecting config.

        Includes: strategy_name, symbols, timeframe, market, data_source, broker,
        account, start, end, params, cost_overrides, symbol_cost_overrides,
        instrument_overrides, execution, risk.
        Excludes: runtime behavior.
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
                    "account": asdict(self.account),
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
