"""Durable runtime state for simulation and live execution.

The engine owns state transitions; stores only provide atomic load/save.
Broker order history is written separately from the active-order checkpoint so
completed orders do not make the checkpoint grow without bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Protocol

from librae.core.run_config import LiveMode
from librae.core.strategy import (
    OrderIntent,
    PortfolioWeights,
    PositionState,
)

from .executor import OrderRequest, OrderStatus


def _to_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("runtime-state timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _timestamps_from_dict(raw: dict, *, field: str) -> dict[str, datetime]:
    timestamps: dict[str, datetime] = {}
    for symbol, value in raw.items():
        timestamp = _to_utc(value)
        if timestamp is None:
            raise ValueError(f"{field}[{symbol!r}] must contain a timestamp")
        timestamps[str(symbol)] = timestamp
    return timestamps


# Bump whenever this document or a persisted nested dataclass changes shape.
# Old checkpoints are deliberately rejected instead of silently defaulted.
_STATE_SCHEMA_VERSION = 18


def normalize_runtime_revision(
    runtime_revision: str | None,
    *,
    required: bool = False,
) -> str | None:
    """Normalize one caller-owned opaque runtime identity."""
    if runtime_revision is None:
        if required:
            raise ValueError("live mode requires a non-empty runtime_revision")
        return None
    if not isinstance(runtime_revision, str):
        raise TypeError("runtime_revision must be a string or None")
    normalized = runtime_revision.strip()
    if not normalized:
        raise ValueError("runtime_revision must be a non-empty string or None")
    return normalized


def _pending_intents_to_list(pending_intents: list[OrderIntent]) -> list[dict]:
    """pending_decision is always plain OrderIntents: PortfolioWeights and
    grouped OrderIntents (group_id is not None) must be immediately
    executable when returned and never sit waiting across periods (see
    executor.validate_strategy_decision).
    """
    return [asdict(intent) for intent in pending_intents]


def _pending_intents_from_list(raw: list) -> list[OrderIntent]:
    return [OrderIntent(**item) for item in raw]


@dataclass
class TrackedOrder:
    """A broker order plus the cumulative fill already applied locally."""

    request: OrderRequest
    placement_attempted: bool = False
    placement_attempted_at: datetime | None = None
    order_id: str = ""
    status: OrderStatus = "submitted"
    filled_quantity: float = 0.0
    filled_notional: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    tax: float = 0.0
    executed_at: datetime | None = None

    def to_dict(self) -> dict:
        request = asdict(self.request)
        request["submitted_at"] = self.request.submitted_at.isoformat()
        return {
            "request": request,
            "placement_attempted": self.placement_attempted,
            "placement_attempted_at": (
                self.placement_attempted_at.isoformat() if self.placement_attempted_at else None
            ),
            "order_id": self.order_id,
            "status": self.status,
            "filled_quantity": self.filled_quantity,
            "filled_notional": self.filled_notional,
            "commission": self.commission,
            "slippage": self.slippage,
            "tax": self.tax,
            "executed_at": self.executed_at.isoformat() if self.executed_at else None,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> TrackedOrder:
        request_raw = dict(raw["request"])
        request_raw["submitted_at"] = _to_utc(request_raw["submitted_at"])
        placement_attempted = bool(raw["placement_attempted"])
        placement_attempted_at = _to_utc(raw["placement_attempted_at"])
        if placement_attempted and placement_attempted_at is None:
            raise ValueError("placement-attempted order is missing placement_attempted_at")
        if not placement_attempted and placement_attempted_at is not None:
            raise ValueError("unattempted order cannot have placement_attempted_at")
        return cls(
            request=OrderRequest(**request_raw),
            placement_attempted=placement_attempted,
            placement_attempted_at=placement_attempted_at,
            order_id=str(raw["order_id"] or ""),
            status=raw["status"],
            filled_quantity=float(raw["filled_quantity"]),
            filled_notional=float(raw["filled_notional"]),
            commission=float(raw["commission"]),
            slippage=float(raw["slippage"]),
            tax=float(raw["tax"]),
            executed_at=_to_utc(raw["executed_at"]),
        )


@dataclass
class LiveRebalance:
    """Restartable live target execution using one confirmed leg at a time."""

    targets: PortfolioWeights
    reference_prices: dict[str, float]
    reference_volumes: dict[str, float | None]
    lagged_adv_by_symbol: dict[str, float]
    decided_at: datetime
    next_sequence: int = 0
    filled_bar_quantity_by_symbol: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "targets": {"weights": dict(self.targets.weights), "reason": self.targets.reason},
            "reference_prices": self.reference_prices,
            "reference_volumes": self.reference_volumes,
            "lagged_adv_by_symbol": self.lagged_adv_by_symbol,
            "decided_at": self.decided_at.isoformat(),
            "next_sequence": self.next_sequence,
            "filled_bar_quantity_by_symbol": self.filled_bar_quantity_by_symbol,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveRebalance:
        targets = PortfolioWeights(**raw["targets"])
        decided_at = _to_utc(raw["decided_at"])
        if decided_at is None:
            raise ValueError("live rebalance is missing decided_at")
        next_sequence = int(raw["next_sequence"])
        if next_sequence < 0:
            raise ValueError("live rebalance next_sequence must be non-negative")
        return cls(
            targets=targets,
            reference_prices={
                str(symbol): float(price) for symbol, price in raw["reference_prices"].items()
            },
            reference_volumes={
                str(symbol): (float(volume) if volume is not None else None)
                for symbol, volume in raw["reference_volumes"].items()
            },
            lagged_adv_by_symbol={
                str(symbol): float(value) for symbol, value in raw["lagged_adv_by_symbol"].items()
            },
            decided_at=decided_at,
            next_sequence=next_sequence,
            filled_bar_quantity_by_symbol={
                str(symbol): float(quantity)
                for symbol, quantity in raw["filled_bar_quantity_by_symbol"].items()
            },
        )


@dataclass
class LiveRuntimeState:
    """One restartable strategy deployment checkpoint."""

    state_key: str
    run_id: str
    config_hash: str
    mode: LiveMode
    account_id: str
    cash: float
    runtime_revision: str | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    last_cycle_ts: datetime | None = None
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    last_funding_ts: dict[str, datetime] = field(default_factory=dict)
    pending_decision: list[OrderIntent] = field(default_factory=list)
    active_orders: list[TrackedOrder] = field(default_factory=list)
    live_rebalance: LiveRebalance | None = None
    equity_peak: float = 0.0
    prev_equity: float = 0.0
    trade_count: int = 0
    event_sequence: int = 0
    period_index: int = 0
    status_period_count: int = 0
    halted: bool = False
    adv_session_labels: dict[str, str] = field(default_factory=dict)
    adv_filled_quantities: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not self.account_id:
            raise ValueError("live runtime state requires a non-empty account_id")
        self.runtime_revision = normalize_runtime_revision(
            self.runtime_revision,
            required=self.mode == "live",
        )
        if any(not isfinite(value) for value in (self.cash, self.equity_peak, self.prev_equity)):
            raise ValueError("live runtime account values must be finite")

    def to_dict(self) -> dict:
        positions: dict[str, dict] = {}
        for symbol, position in self.positions.items():
            item = asdict(position)
            item["entry_at"] = position.entry_at.isoformat()
            positions[symbol] = item
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "state_key": self.state_key,
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "mode": self.mode,
            "account_id": self.account_id,
            "runtime_revision": self.runtime_revision,
            "cash": self.cash,
            "positions": positions,
            "last_prices": self.last_prices,
            "last_cycle_ts": self.last_cycle_ts.isoformat() if self.last_cycle_ts else None,
            "last_bar_ts": {
                symbol: timestamp.isoformat() for symbol, timestamp in self.last_bar_ts.items()
            },
            "last_funding_ts": {
                symbol: timestamp.isoformat() for symbol, timestamp in self.last_funding_ts.items()
            },
            "pending_decision": _pending_intents_to_list(self.pending_decision),
            "active_orders": [order.to_dict() for order in self.active_orders],
            "live_rebalance": self.live_rebalance.to_dict() if self.live_rebalance else None,
            "equity_peak": self.equity_peak,
            "prev_equity": self.prev_equity,
            "trade_count": self.trade_count,
            "event_sequence": self.event_sequence,
            "period_index": self.period_index,
            "status_period_count": self.status_period_count,
            "halted": self.halted,
            "adv_session_labels": self.adv_session_labels,
            "adv_filled_quantities": self.adv_filled_quantities,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveRuntimeState:
        schema_version = raw.get("schema_version")
        if schema_version != _STATE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported live runtime-state schema: "
                f"expected {_STATE_SCHEMA_VERSION}, got {schema_version!r}"
            )
        positions = {}
        for symbol, item in raw["positions"].items():
            position_raw = dict(item)
            position_raw["entry_at"] = _to_utc(position_raw["entry_at"])
            positions[symbol] = PositionState(**position_raw)
        return cls(
            state_key=str(raw["state_key"]),
            run_id=str(raw["run_id"]),
            config_hash=str(raw["config_hash"]),
            mode=raw["mode"],
            account_id=str(raw["account_id"]),
            runtime_revision=raw["runtime_revision"],
            cash=float(raw["cash"]),
            positions=positions,
            last_prices={str(symbol): float(price) for symbol, price in raw["last_prices"].items()},
            last_cycle_ts=_to_utc(raw["last_cycle_ts"]),
            last_bar_ts=_timestamps_from_dict(raw["last_bar_ts"], field="last_bar_ts"),
            last_funding_ts=_timestamps_from_dict(
                raw["last_funding_ts"],
                field="last_funding_ts",
            ),
            pending_decision=_pending_intents_from_list(raw["pending_decision"]),
            active_orders=[TrackedOrder.from_dict(item) for item in raw["active_orders"]],
            live_rebalance=(
                LiveRebalance.from_dict(raw["live_rebalance"])
                if raw["live_rebalance"] is not None
                else None
            ),
            equity_peak=float(raw["equity_peak"]),
            prev_equity=float(raw["prev_equity"]),
            trade_count=int(raw["trade_count"]),
            event_sequence=int(raw["event_sequence"]),
            period_index=int(raw["period_index"]),
            status_period_count=int(raw["status_period_count"]),
            halted=bool(raw["halted"]),
            adv_session_labels={
                str(symbol): str(label) for symbol, label in raw["adv_session_labels"].items()
            },
            adv_filled_quantities={
                str(symbol): float(quantity)
                for symbol, quantity in raw["adv_filled_quantities"].items()
            },
        )


class LiveStateStore(Protocol):
    """Minimal persistence boundary used by ``LiveTrader``."""

    def load(self, state_key: str) -> LiveRuntimeState | None: ...

    def save(
        self,
        state: LiveRuntimeState,
        orders: Sequence[TrackedOrder] = (),
    ) -> None: ...

    def acquire_lease(self, state_key: str) -> bool: ...

    def release_lease(self, state_key: str) -> None: ...


class MemoryLiveStateStore:
    """Process-local store for deterministic tests; not restart durability."""

    def __init__(self) -> None:
        self._states: dict[str, LiveRuntimeState] = {}
        self.orders: dict[str, TrackedOrder] = {}
        self._leases: set[str] = set()

    def load(self, state_key: str) -> LiveRuntimeState | None:
        state = self._states.get(state_key)
        return deepcopy(state) if state else None

    def save(
        self,
        state: LiveRuntimeState,
        orders: Sequence[TrackedOrder] = (),
    ) -> None:
        self._states[state.state_key] = deepcopy(state)
        for order in orders:
            self.orders[order.request.client_order_id] = deepcopy(order)

    def acquire_lease(self, state_key: str) -> bool:
        if state_key in self._leases:
            return False
        self._leases.add(state_key)
        return True

    def release_lease(self, state_key: str) -> None:
        self._leases.discard(state_key)
