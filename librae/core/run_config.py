"""Unified run configuration — single source of truth for all execution paths.

RunConfig is a frozen dataclass that holds all parameters for a run:
- Strategy params (stored in DB backtest_runs.params)
- Perf params (stored in DB backtest_runs.perf_params, display only)
- Behavior params (not stored in DB)

The sole factory is build_config() in orchestration/cli.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Literal

logger = logging.getLogger(__name__)


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

    Created exclusively by build_config() in orchestration/cli.py.
    Passed through: CLI -> engine -> DB writer.
    """

    # === Strategy identification (stored in DB) ===
    strategy_name: str
    symbols: list[str]
    timeframe: str
    market: str
    data_source: str
    initial_balance: float
    mode: Literal["backtest", "sim", "live"]
    # Explicit live execution route. It is never inferred from market,
    # data_source, or symbol; instrument_overrides[symbol]["broker"] wins.
    broker: str | None = None
    start: str | None = None
    end: str | None = None
    params: dict[str, Any] | None = None
    # Cost-model overrides. cost_overrides applies to every symbol in this
    # run (falls back to the built-in symbol/market registries for anything not listed);
    # symbol_overrides applies to one symbol only and wins over
    # cost_overrides for that symbol — see CostModel.from_config(). This is
    # the escape hatch for a symbol that isn't in the built-in registry
    # (no file to edit, no path to point at — just pass
    # {"MYSYM": {"multiplier": 1.0}}) and for multi-asset runs mixing
    # symbols with different multipliers (e.g. TXFR1=200 + MXFR1=50 in the
    # same tw_futures run).
    cost_overrides: dict[str, float] | None = None
    symbol_overrides: dict[str, dict[str, float]] | None = None
    # Broker/data routing metadata for one symbol. Cost fields remain in
    # symbol_overrides so accounting inputs and venue identifiers cannot be
    # accidentally mixed into CostModel construction.
    instrument_overrides: dict[str, dict[str, str]] | None = None

    # === Perf params (stored in DB backtest_runs.perf_params, display only) ===
    annualize: bool = True
    risk_free_rate: float = 0.0
    annual_periods: int = 365

    # === Behavior params (not stored in DB) ===
    poll_seconds: int = 60
    no_db: bool = False
    dry_run: bool = False
    force: bool = False
    telegram_config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """Validate invariants. Raise, never mutate (frozen purity)."""
        if not self.symbols or any(not symbol for symbol in self.symbols):
            raise ValueError("symbols must contain non-empty identifiers")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("symbols must not contain duplicates")
        if not self.market or not self.data_source:
            raise ValueError("market and data_source must be non-empty")
        if self.dry_run and not self.no_db:
            raise ValueError("dry_run=True requires no_db=True; use build_config()")

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
            "annual_periods": self.annual_periods,
        }

    @cached_property
    def config_hash(self) -> str:
        """Deterministic hash of all result-affecting config.

        Includes: strategy_name, symbols, timeframe, market, data_source, broker,
        initial_balance, start, end, params, cost_overrides, symbol_overrides,
        instrument_overrides.
        Excludes: perf params, behavior params.
        """
        blob = json.dumps(
            _sanitize_for_hash(
                {
                    "strategy_name": self.strategy_name,
                    "symbols": sorted(self.symbols),
                    "timeframe": self.timeframe,
                    "market": self.market,
                    "data_source": self.data_source,
                    "broker": self.broker,
                    "initial_balance": self.initial_balance,
                    "start": self.start,
                    "end": self.end,
                    "params": self.params,
                    "cost_overrides": self.cost_overrides,
                    "symbol_overrides": self.symbol_overrides,
                    "instrument_overrides": self.instrument_overrides,
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
            f"  params:      {self.params}",
            f"  cost_overrides: {self.cost_overrides}",
            f"  symbol_overrides: {self.symbol_overrides}",
            f"  instrument_overrides: {self.instrument_overrides}",
            "  --- perf params (stored in DB, display only) ---",
            f"  annualize:   {self.annualize}",
            f"  risk_free_rate: {self.risk_free_rate}",
            f"  annual_periods: {self.annual_periods}",
            "  --- behavior (not stored in DB) ---",
            f"  no_db:       {self.no_db}",
            f"  dry_run:     {self.dry_run}",
            f"  force:       {self.force}",
            f"  poll_seconds: {self.poll_seconds}",
            f"  telegram:    {masked_tg}",
            "=" * 60,
        ]
        logger.info("\n".join(lines))
