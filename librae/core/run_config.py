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
from typing import Any, Literal, cast

from librae.core.utils import to_canonical

RunMode = Literal["backtest", "sim", "live"]
LiveMode = Literal["sim", "live"]
RebalanceResidualPolicy = Literal["discard", "fail", "defer_all", "defer_symbols"]
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
    fill. A simulated close fill uses that completed bar's volume. Open,
    limit, other intrabar fills, and protective exits use the previous
    completed bar's volume so later information cannot change an earlier fill.
    Forced end-of-run exits fill at the completed final close and use its volume.

    ``adv_lookback_sessions`` and ``max_adv_participation_rate`` form one
    optional session-level capacity limit. ADV uses exactly N completed
    sessions, excluding the execution session. Intraday data therefore needs
    a calendar_id for every configured symbol.

    ``max_rebalance_delay_bars`` bounds how many otherwise eligible backtest
    or live events a ``PortfolioWeights`` decision may wait for every required
    order side and coherent completed-bar snapshot. Zero preserves fail-fast
    execution. In live mode, broker-resting time remains governed separately
    by ``live_order_timeout_seconds``.

    ``rebalance_residual_policy`` opts portfolio targets into cross-bar
    execution slicing. ``discard`` preserves the one-shot compatibility
    behavior; ``fail`` rejects any incomplete staged rebalance without partial
    mutation; ``defer_all`` waits whenever any residual symbol cannot execute,
    while ``defer_symbols`` lets independently executable symbols make progress.

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
    max_rebalance_delay_bars: int = 0
    live_order_timeout_seconds: int | None = None
    warmup_periods: int = 720
    rebalance_residual_policy: RebalanceResidualPolicy = "discard"

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
        if (
            isinstance(self.max_rebalance_delay_bars, bool)
            or not isinstance(self.max_rebalance_delay_bars, int)
            or self.max_rebalance_delay_bars < 0
        ):
            raise ValueError("max_rebalance_delay_bars must be a non-negative integer")
        if self.rebalance_residual_policy not in (
            "discard",
            "fail",
            "defer_all",
            "defer_symbols",
        ):
            raise ValueError(
                "rebalance_residual_policy must be 'discard', 'fail', "
                f"'defer_all', or 'defer_symbols', got {self.rebalance_residual_policy!r}"
            )
        if (
            self.rebalance_residual_policy
            in (
                "defer_all",
                "defer_symbols",
            )
            and self.max_rebalance_delay_bars == 0
        ):
            raise ValueError(
                "deferred rebalance_residual_policy requires positive max_rebalance_delay_bars"
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


class FrozenDict(dict[str, object]):
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


def _freeze(value: object, *, path: str) -> object:
    """Validate and detach one caller-owned JSON-like config value."""
    value_type = type(value)
    if value_type is dict or value_type is FrozenDict:
        mapping = cast(dict[object, object], value)
        frozen: dict[str, object] = {}
        for key, item in mapping.items():
            if type(key) is not str:
                raise TypeError(f"{path} mapping keys must be strings")
            string_key = cast(str, key)
            frozen[string_key] = _freeze(item, path=f"{path}[{string_key!r}]")
        return FrozenDict(frozen)
    if value_type is list or value_type is tuple:
        sequence = cast(list[object] | tuple[object, ...], value)
        return tuple(_freeze(item, path=f"{path}[{index}]") for index, item in enumerate(sequence))
    if value is None or value_type is bool or value_type is int or value_type is str:
        return value
    if value_type is float:
        number = cast(float, value)
        if not isfinite(number):
            raise ValueError(f"{path} float values must be finite")
        return number
    raise TypeError(
        f"{path} contains unsupported {value_type.__name__}; "
        "expected JSON-like scalars, dictionaries, lists, or tuples"
    )


def _canonicalize_for_hash(obj: object) -> object:
    """Encode config values with explicit type tags for a stable hash."""
    if obj is None:
        return ("null",)
    if isinstance(obj, bool):
        return ("bool", obj)
    if isinstance(obj, int):
        return ("int", str(obj))
    if isinstance(obj, float):
        if not isfinite(obj):
            raise ValueError("RunConfig float values must be finite")
        return ("float", obj.hex())
    if isinstance(obj, str):
        return ("str", str(obj))
    if isinstance(obj, dict):
        return (
            "mapping",
            [(key, _canonicalize_for_hash(value)) for key, value in sorted(obj.items())],
        )
    if isinstance(obj, (list, tuple)):
        return ("sequence", [_canonicalize_for_hash(value) for value in obj])
    raise TypeError(f"RunConfig contains unsupported {type(obj).__name__}")


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
    cost_overrides: dict[str, float | str] | None = None
    symbol_cost_overrides: dict[str, dict[str, float | str]] | None = None
    # Broker/data routing metadata for one symbol. Cost fields remain in
    # symbol_cost_overrides so accounting inputs and venue identifiers cannot be
    # accidentally mixed into CostModel construction.
    instrument_overrides: dict[str, dict[str, object]] | None = None
    # Run-wide trading-session calendar fallback, mirroring market/data_source:
    # resolve_symbol() uses it for any symbol without its own registry entry or
    # instrument_overrides[symbol]["calendar_id"]. Lets a homogeneous, dynamically
    # discovered universe (e.g. a screening strategy) share one calendar without
    # enumerating every symbol up front. None if unset — calendar_id stays optional
    # per SymbolInfo and is only required where session-boundary awareness is
    # actually used (intraday ADV, session-aware resampling).
    calendar_id: str | None = None

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
                if type(value) is not dict and type(value) is not FrozenDict:
                    raise TypeError(f"{field_name} must be a dictionary or None")
                object.__setattr__(
                    self,
                    field_name,
                    _freeze(value, path=f"RunConfig.{field_name}"),
                )

        if not self.symbols or any(
            not isinstance(symbol, str) or not symbol for symbol in self.symbols
        ):
            raise ValueError("symbols must contain non-empty string identifiers")
        if self.mode not in ("backtest", "sim", "live"):
            raise ValueError(f"mode must be 'backtest', 'sim', or 'live', got {self.mode!r}")
        if self.mode == "sim" and (
            self.execution.max_rebalance_delay_bars
            or self.execution.rebalance_residual_policy != "discard"
        ):
            raise ValueError(
                "deferred portfolio rebalancing is supported only when mode='backtest'"
            )
        if self.mode == "live" and self.execution.rebalance_residual_policy != "discard":
            raise ValueError("rebalance_residual_policy is supported only when mode='backtest'")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("symbols must not contain duplicates")
        for field_name in ("strategy_name", "timeframe", "market", "data_source"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        object.__setattr__(self, "timeframe", to_canonical(self.timeframe))
        for field_name in ("start", "end"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        if self.broker is not None and (not isinstance(self.broker, str) or not self.broker):
            raise ValueError("broker must be a non-empty string or None")
        if self.calendar_id is not None and (
            not isinstance(self.calendar_id, str) or not self.calendar_id
        ):
            raise ValueError("calendar_id must be a non-empty string or None")
        legacy_execution_keys = {
            "fill_price",
            "max_volume_participation_pct",
            "max_volume_participation_rate",
            "max_bar_volume_participation_rate",
            "adv_lookback_sessions",
            "max_adv_participation_rate",
            "max_rebalance_delay_bars",
            "rebalance_residual_policy",
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
        calendar_id, account, start, end, params, cost_overrides,
        symbol_cost_overrides, instrument_overrides, execution, risk.
        Excludes: runtime behavior.
        """
        blob = json.dumps(
            _canonicalize_for_hash(
                {
                    "strategy_name": self.strategy_name,
                    # Primary-symbol order is observable engine behaviour.
                    "symbols": self.symbols,
                    "timeframe": self.timeframe,
                    "market": self.market,
                    "data_source": self.data_source,
                    "mode": self.mode,
                    "broker": self.broker,
                    "calendar_id": self.calendar_id,
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
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:32]
