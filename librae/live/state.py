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
from typing import Protocol

from librae.core.run_config import LiveMode
from librae.core.strategy import OrderIntent, PortfolioTargets, PositionState, StrategyDecision

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


_STATE_SCHEMA_VERSION = 4


def _decision_to_dict(decision: StrategyDecision) -> dict:
    if isinstance(decision, PortfolioTargets):
        return {"kind": "portfolio_targets", "value": asdict(decision)}
    return {"kind": "order_intents", "value": [asdict(intent) for intent in decision]}


def _decision_from_dict(raw: dict) -> StrategyDecision:
    kind = raw.get("kind")
    value = raw.get("value")
    if kind == "portfolio_targets" and isinstance(value, dict):
        return PortfolioTargets(**value)
    if kind == "order_intents" and isinstance(value, list):
        return [OrderIntent(**item) for item in value]
    raise ValueError("invalid persisted strategy decision")


@dataclass
class TrackedOrder:
    """A broker order plus the cumulative fill already applied locally."""

    request: OrderRequest
    placement_attempted: bool = False
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
        return cls(
            request=OrderRequest(**request_raw),
            placement_attempted=bool(raw["placement_attempted"]),
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
class LiveRuntimeState:
    """One restartable strategy deployment checkpoint."""

    state_key: str
    run_id: str
    config_hash: str
    mode: LiveMode
    cash: float
    positions: dict[str, PositionState] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    last_cycle_ts: datetime | None = None
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    pending_decision: StrategyDecision = field(default_factory=list)
    active_orders: list[TrackedOrder] = field(default_factory=list)
    equity_peak: float = 0.0
    prev_equity: float = 0.0
    trade_count: int = 0
    event_sequence: int = 0
    period_index: int = 0
    status_period_count: int = 0
    halted: bool = False

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
            "cash": self.cash,
            "positions": positions,
            "last_prices": self.last_prices,
            "last_cycle_ts": self.last_cycle_ts.isoformat() if self.last_cycle_ts else None,
            "last_bar_ts": {
                symbol: timestamp.isoformat() for symbol, timestamp in self.last_bar_ts.items()
            },
            "pending_decision": _decision_to_dict(self.pending_decision),
            "active_orders": [order.to_dict() for order in self.active_orders],
            "equity_peak": self.equity_peak,
            "prev_equity": self.prev_equity,
            "trade_count": self.trade_count,
            "event_sequence": self.event_sequence,
            "period_index": self.period_index,
            "status_period_count": self.status_period_count,
            "halted": self.halted,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> LiveRuntimeState:
        if raw.get("schema_version") != _STATE_SCHEMA_VERSION:
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
            cash=float(raw["cash"]),
            positions=positions,
            last_prices={str(symbol): float(price) for symbol, price in raw["last_prices"].items()},
            last_cycle_ts=_to_utc(raw["last_cycle_ts"]),
            last_bar_ts=_bar_timestamps_from_dict(raw["last_bar_ts"]),
            pending_decision=_decision_from_dict(raw["pending_decision"]),
            active_orders=[TrackedOrder.from_dict(item) for item in raw["active_orders"]],
            equity_peak=float(raw["equity_peak"]),
            prev_equity=float(raw["prev_equity"]),
            trade_count=int(raw["trade_count"]),
            event_sequence=int(raw["event_sequence"]),
            period_index=int(raw["period_index"]),
            status_period_count=int(raw["status_period_count"]),
            halted=bool(raw["halted"]),
        )


class LiveStateStore(Protocol):
    """Minimal persistence boundary used by ``LiveTrader``."""

    def load(self, state_key: str) -> LiveRuntimeState | None: ...

    def save(
        self,
        state: LiveRuntimeState,
        orders: Sequence[TrackedOrder] = (),
    ) -> None: ...


class MemoryLiveStateStore:
    """Process-local store for deterministic tests; not restart durability."""

    def __init__(self) -> None:
        self._states: dict[str, LiveRuntimeState] = {}
        self.orders: dict[str, TrackedOrder] = {}

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
