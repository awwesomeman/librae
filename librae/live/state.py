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
from typing import Literal, Protocol

from librae.core.run_config import LiveMode
from librae.core.strategy import (
    MultiLegOrder,
    OrderIntent,
    PortfolioTargets,
    PositionState,
    StrategyDecision,
)

from .executor import OrderRequest, OrderStatus


def _to_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("runtime-state timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _bar_timestamps_from_dict(raw: dict) -> dict[str, datetime]:
    timestamps: dict[str, datetime] = {}
    for symbol, value in raw.items():
        timestamp = _to_utc(value)
        if timestamp is None:
            raise ValueError(f"last_bar_ts[{symbol!r}] must contain a timestamp")
        timestamps[str(symbol)] = timestamp
    return timestamps


_STATE_SCHEMA_VERSION = 10


def _decision_to_dict(decision: StrategyDecision) -> dict:
    if isinstance(decision, PortfolioTargets):
        return {
            "kind": "portfolio_targets",
            "value": {
                "weights": dict(decision.weights),
                "fill_price": decision.fill_price,
                "reason": decision.reason,
            },
        }
    if isinstance(decision, MultiLegOrder):
        return {
            "kind": "multi_leg_order",
            "value": {
                "legs": [asdict(leg) for leg in decision.legs],
                "max_completion_seconds": decision.max_completion_seconds,
                "reason": decision.reason,
            },
        }
    return {"kind": "order_intents", "value": [asdict(intent) for intent in decision]}


def _decision_from_dict(raw: dict) -> StrategyDecision:
    kind = raw.get("kind")
    value = raw.get("value")
    if kind == "portfolio_targets" and isinstance(value, dict):
        return PortfolioTargets(**value)
    if kind == "order_intents" and isinstance(value, list):
        return [OrderIntent(**item) for item in value]
    if kind == "multi_leg_order" and isinstance(value, dict):
        return MultiLegOrder(
            legs=tuple(OrderIntent(**item) for item in value["legs"]),
            max_completion_seconds=float(value["max_completion_seconds"]),
            reason=str(value["reason"]),
        )
    raise ValueError("invalid persisted strategy decision")


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

    targets: PortfolioTargets
    reference_prices: dict[str, float]
    reference_volumes: dict[str, float | None]
    lagged_adv_by_symbol: dict[str, float]
    decided_at: datetime
    next_sequence: int = 0
    filled_bar_quantity_by_symbol: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "targets": _decision_to_dict(self.targets),
            "reference_prices": self.reference_prices,
            "reference_volumes": self.reference_volumes,
            "lagged_adv_by_symbol": self.lagged_adv_by_symbol,
            "decided_at": self.decided_at.isoformat(),
            "next_sequence": self.next_sequence,
            "filled_bar_quantity_by_symbol": self.filled_bar_quantity_by_symbol,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveRebalance:
        targets = _decision_from_dict(raw["targets"])
        if not isinstance(targets, PortfolioTargets):
            raise ValueError("live rebalance must contain PortfolioTargets")
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
class LiveMultiLeg:
    """Restartable best-effort multi-leg execution and baseline restoration."""

    order: MultiLegOrder
    baseline_signed_quantities: dict[str, float]
    reference_prices: dict[str, float]
    reference_volumes: dict[str, float | None]
    lagged_adv_by_symbol: dict[str, float]
    decided_at: datetime
    next_leg_index: int = 0
    first_fill_at: datetime | None = None
    phase: Literal["executing", "restoring", "manual"] = "executing"

    def to_dict(self) -> dict:
        return {
            "order": _decision_to_dict(self.order),
            "baseline_signed_quantities": self.baseline_signed_quantities,
            "reference_prices": self.reference_prices,
            "reference_volumes": self.reference_volumes,
            "lagged_adv_by_symbol": self.lagged_adv_by_symbol,
            "decided_at": self.decided_at.isoformat(),
            "next_leg_index": self.next_leg_index,
            "first_fill_at": (self.first_fill_at.isoformat() if self.first_fill_at else None),
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveMultiLeg:
        order = _decision_from_dict(raw["order"])
        if not isinstance(order, MultiLegOrder):
            raise ValueError("live multi-leg state must contain MultiLegOrder")
        decided_at = _to_utc(raw["decided_at"])
        if decided_at is None:
            raise ValueError("live multi-leg state is missing decided_at")
        phase = str(raw["phase"])
        if phase not in ("executing", "restoring", "manual"):
            raise ValueError(f"invalid live multi-leg phase: {phase!r}")
        next_leg_index = int(raw["next_leg_index"])
        if not 0 <= next_leg_index <= len(order.legs):
            raise ValueError("live multi-leg next_leg_index is out of range")
        baseline_signed_quantities = {
            str(symbol): float(quantity)
            for symbol, quantity in raw["baseline_signed_quantities"].items()
        }
        leg_symbols = {leg.symbol for leg in order.legs}
        if set(baseline_signed_quantities) != leg_symbols:
            raise ValueError("live multi-leg baseline must cover exactly the leg symbols")
        if any(not isfinite(quantity) for quantity in baseline_signed_quantities.values()):
            raise ValueError("live multi-leg baseline quantities must be finite")
        return cls(
            order=order,
            baseline_signed_quantities=baseline_signed_quantities,
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
            next_leg_index=next_leg_index,
            first_fill_at=_to_utc(raw["first_fill_at"]),
            phase=phase,
        )


@dataclass
class LiveRuntimeState:
    """One restartable strategy deployment checkpoint."""

    state_key: str
    run_id: str
    config_hash: str
    mode: LiveMode
    cash_by_account: dict[str, float]
    positions: dict[str, PositionState] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    last_cycle_ts: datetime | None = None
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    pending_decision: StrategyDecision = field(default_factory=list)
    active_orders: list[TrackedOrder] = field(default_factory=list)
    live_rebalance: LiveRebalance | None = None
    live_multi_leg: LiveMultiLeg | None = None
    equity_peak_by_account: dict[str, float] = field(default_factory=dict)
    prev_equity_by_account: dict[str, float] = field(default_factory=dict)
    trade_count: int = 0
    event_sequence: int = 0
    period_index: int = 0
    status_period_count: int = 0
    halted: bool = False
    halted_accounts: set[str] = field(default_factory=set)
    adv_session_labels: dict[str, str] = field(default_factory=dict)
    adv_filled_quantities: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        account_ids = set(self.cash_by_account)
        if not account_ids or any(not account_id for account_id in account_ids):
            raise ValueError("live runtime state requires non-empty account ids")
        if account_ids != set(self.equity_peak_by_account) or account_ids != set(
            self.prev_equity_by_account
        ):
            raise ValueError("live runtime account cash/equity keys must match")
        if not self.halted_accounts <= account_ids:
            raise ValueError("halted accounts must belong to the runtime state")
        values = (
            *self.cash_by_account.values(),
            *self.equity_peak_by_account.values(),
            *self.prev_equity_by_account.values(),
        )
        if any(not isfinite(value) for value in values):
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
            "cash_by_account": self.cash_by_account,
            "positions": positions,
            "last_prices": self.last_prices,
            "last_cycle_ts": self.last_cycle_ts.isoformat() if self.last_cycle_ts else None,
            "last_bar_ts": {
                symbol: timestamp.isoformat() for symbol, timestamp in self.last_bar_ts.items()
            },
            "pending_decision": _decision_to_dict(self.pending_decision),
            "active_orders": [order.to_dict() for order in self.active_orders],
            "live_rebalance": self.live_rebalance.to_dict() if self.live_rebalance else None,
            "live_multi_leg": (self.live_multi_leg.to_dict() if self.live_multi_leg else None),
            "equity_peak_by_account": self.equity_peak_by_account,
            "prev_equity_by_account": self.prev_equity_by_account,
            "trade_count": self.trade_count,
            "event_sequence": self.event_sequence,
            "period_index": self.period_index,
            "status_period_count": self.status_period_count,
            "halted": self.halted,
            "halted_accounts": sorted(self.halted_accounts),
            "adv_session_labels": self.adv_session_labels,
            "adv_filled_quantities": self.adv_filled_quantities,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveRuntimeState:
        schema_version = raw.get("schema_version")
        if schema_version not in (9, _STATE_SCHEMA_VERSION):
            raise ValueError("unsupported live runtime-state schema")
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
            cash_by_account={
                str(account_id): float(cash) for account_id, cash in raw["cash_by_account"].items()
            },
            positions=positions,
            last_prices={str(symbol): float(price) for symbol, price in raw["last_prices"].items()},
            last_cycle_ts=_to_utc(raw["last_cycle_ts"]),
            last_bar_ts=_bar_timestamps_from_dict(raw["last_bar_ts"]),
            pending_decision=_decision_from_dict(raw["pending_decision"]),
            active_orders=[TrackedOrder.from_dict(item) for item in raw["active_orders"]],
            live_rebalance=(
                LiveRebalance.from_dict(raw["live_rebalance"])
                if raw["live_rebalance"] is not None
                else None
            ),
            live_multi_leg=(
                LiveMultiLeg.from_dict(raw["live_multi_leg"])
                if raw["live_multi_leg"] is not None
                else None
            ),
            equity_peak_by_account={
                str(account_id): float(equity)
                for account_id, equity in raw["equity_peak_by_account"].items()
            },
            prev_equity_by_account={
                str(account_id): float(equity)
                for account_id, equity in raw["prev_equity_by_account"].items()
            },
            trade_count=int(raw["trade_count"]),
            event_sequence=int(raw["event_sequence"]),
            period_index=int(raw["period_index"]),
            status_period_count=int(raw["status_period_count"]),
            halted=bool(raw["halted"]),
            halted_accounts={str(account_id) for account_id in raw.get("halted_accounts", [])},
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
