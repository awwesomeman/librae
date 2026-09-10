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

from librae.core.market_data import MarketDataSubscription
from librae.core.run_config import LiveMode
from librae.core.strategy import (
    OrderIntent,
    PortfolioWeights,
    PositionState,
    StrategyDecision,
)

from .execution_identity import ExecutionIdentity
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
_STATE_SCHEMA_VERSION = 27


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


def _pending_decision_to_dict(decision: StrategyDecision) -> dict:
    """Serialize the two public strategy-decision variants explicitly."""
    if isinstance(decision, PortfolioWeights):
        return {
            "type": "portfolio_weights",
            "weights": {symbol: float(weight) for symbol, weight in decision.weights.items()},
            "reason": decision.reason,
        }
    if not isinstance(decision, list) or not all(
        isinstance(intent, OrderIntent) for intent in decision
    ):
        raise TypeError("pending_decision must be OrderIntents or PortfolioWeights")
    return {
        "type": "order_intents",
        "intents": [asdict(intent) for intent in decision],
    }


def _pending_decision_from_dict(raw: dict) -> StrategyDecision:
    """Restore a tagged strategy decision without guessing malformed shapes."""
    if not isinstance(raw, dict):
        raise TypeError("pending_decision must be an object")
    decision_type = raw.get("type")
    if decision_type == "order_intents":
        if set(raw) != {"type", "intents"}:
            raise ValueError("malformed order_intents pending decision")
        intents = raw["intents"]
        if not isinstance(intents, list) or not all(isinstance(item, dict) for item in intents):
            raise TypeError("order_intents pending decision requires a list of objects")
        return [OrderIntent(**item) for item in intents]
    if decision_type == "portfolio_weights":
        if set(raw) != {"type", "weights", "reason"}:
            raise ValueError("malformed portfolio_weights pending decision")
        if not isinstance(raw["weights"], dict):
            raise TypeError("portfolio_weights pending decision requires a weights object")
        return PortfolioWeights(weights=raw["weights"], reason=raw["reason"])
    raise ValueError(f"unknown pending decision type: {decision_type!r}")


@dataclass
class TrackedOrder:
    """A broker order plus the cumulative fill already applied locally."""

    request: OrderRequest
    placement_attempted: bool = False
    placement_attempted_at: datetime | None = None
    order_id: str = ""
    # WHY: status carries only what the broker reported, so it stays inside the
    # vocabulary the durable store constrains. Wanting to cancel is the
    # engine's own state and gets its own field -- unlike a broker-acknowledged
    # ``cancel_pending``, it permits another idempotent cancel-by-order-id call.
    status: OrderStatus = "submitted"
    cancel_requested: bool = False
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
            "cancel_requested": self.cancel_requested,
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
            cancel_requested=bool(raw.get("cancel_requested", False)),
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
    execution_bar_ts: datetime | None = None
    delay_bars: int = 0
    filled_bar_quantity_by_symbol: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "targets": {"weights": dict(self.targets.weights), "reason": self.targets.reason},
            "reference_prices": self.reference_prices,
            "reference_volumes": self.reference_volumes,
            "lagged_adv_by_symbol": self.lagged_adv_by_symbol,
            "decided_at": self.decided_at.isoformat(),
            "next_sequence": self.next_sequence,
            "execution_bar_ts": (
                self.execution_bar_ts.isoformat() if self.execution_bar_ts else None
            ),
            "delay_bars": self.delay_bars,
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
        delay_bars = int(raw["delay_bars"])
        if delay_bars < 0:
            raise ValueError("live rebalance delay_bars must be non-negative")
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
            execution_bar_ts=_to_utc(raw["execution_bar_ts"]),
            delay_bars=delay_bars,
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
    execution_identity: ExecutionIdentity | None = None
    runtime_revision: str | None = None
    positions: dict[str, PositionState] = field(default_factory=dict)
    last_prices: dict[str, float] = field(default_factory=dict)
    last_cycle_ts: datetime | None = None
    last_execution_bar_ts: dict[str, datetime] = field(default_factory=dict)
    execution_bar_filled_quantities: dict[str, float] = field(default_factory=dict)
    last_feature_as_of: datetime | None = None
    last_bar_ts: dict[str, datetime] = field(default_factory=dict)
    last_financing_ts: dict[str, datetime] = field(default_factory=dict)
    pending_decision: StrategyDecision = field(default_factory=list)
    # Bar timestamp each pending intent FIRST rested on, keyed by symbol
    # (validate_strategy_decision allows at most one intent per symbol). A
    # resting "day" limit expires against the session it first rested in, not
    # the one that emitted it — a decision is emitted on one bar and first
    # executable on the next. Only set once an intent has actually rested, and
    # it has to survive a restart along with the intent itself.
    pending_resting_since: dict[str, datetime] = field(default_factory=dict)
    # Audit rows the runtime accepted but the database has not acknowledged.
    # Lands with the watermark it belongs to, so a crash between the two
    # replays the row rather than losing it.
    pending_ohlcv: list[PendingOhlcvDelivery] = field(default_factory=list)
    active_orders: list[TrackedOrder] = field(default_factory=list)
    live_rebalance: LiveRebalance | None = None
    equity_peak: float = 0.0
    prev_equity: float = 0.0
    status_window_equity: float = 0.0
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
        if len(self.pending_ohlcv) > MAX_PENDING_OHLCV:
            raise ValueError(
                f"pending_ohlcv exceeds the durable bound of {MAX_PENDING_OHLCV}; "
                "an unavailable database must surface as terminal runtime health "
                "rather than growing the checkpoint without limit"
            )
        if self.mode == "live" and self.execution_identity is None:
            raise ValueError("live runtime state requires an execution_identity")
        if self.mode != "live" and self.execution_identity is not None:
            raise ValueError("simulation runtime state cannot carry an execution_identity")
        self.last_feature_as_of = _to_utc(self.last_feature_as_of)
        if any(
            not isfinite(value)
            for value in (self.cash, self.equity_peak, self.prev_equity, self.status_window_equity)
        ):
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
            "execution_identity": (
                self.execution_identity.to_dict() if self.execution_identity else None
            ),
            "runtime_revision": self.runtime_revision,
            "cash": self.cash,
            "positions": positions,
            "last_prices": self.last_prices,
            "last_cycle_ts": self.last_cycle_ts.isoformat() if self.last_cycle_ts else None,
            "last_execution_bar_ts": {
                symbol: timestamp.isoformat()
                for symbol, timestamp in self.last_execution_bar_ts.items()
            },
            "execution_bar_filled_quantities": self.execution_bar_filled_quantities,
            "last_feature_as_of": (
                self.last_feature_as_of.isoformat() if self.last_feature_as_of else None
            ),
            "last_bar_ts": {
                symbol: timestamp.isoformat() for symbol, timestamp in self.last_bar_ts.items()
            },
            "last_financing_ts": {
                symbol: timestamp.isoformat()
                for symbol, timestamp in self.last_financing_ts.items()
            },
            "pending_decision": _pending_decision_to_dict(self.pending_decision),
            "pending_ohlcv": [pending.to_dict() for pending in self.pending_ohlcv],
            "pending_resting_since": {
                symbol: timestamp.isoformat()
                for symbol, timestamp in self.pending_resting_since.items()
            },
            "active_orders": [order.to_dict() for order in self.active_orders],
            "live_rebalance": self.live_rebalance.to_dict() if self.live_rebalance else None,
            "equity_peak": self.equity_peak,
            "prev_equity": self.prev_equity,
            "status_window_equity": self.status_window_equity,
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
                "unsupported live runtime-state checkpoint: this build writes "
                f"version {_STATE_SCHEMA_VERSION}, the stored document is "
                f"{schema_version!r}. Checkpoint documents are not migrated "
                "automatically: this one holds positions, cash, in-flight orders "
                "and the halted flag, so a defaulted field would let the local "
                "book disagree with the broker. Stop flat and start a new "
                "checkpoint, or migrate the stored document externally after "
                "reconciling it; see the live migration procedure in "
                "docs/guides/optional-infrastructure.md. This is the checkpoint "
                "document version, not the database schema revision; upgrading "
                "the database schema does not transform a stored checkpoint."
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
            execution_identity=(
                ExecutionIdentity.from_dict(raw["execution_identity"])
                if raw["execution_identity"] is not None
                else None
            ),
            runtime_revision=raw["runtime_revision"],
            cash=float(raw["cash"]),
            positions=positions,
            last_prices={str(symbol): float(price) for symbol, price in raw["last_prices"].items()},
            last_cycle_ts=_to_utc(raw["last_cycle_ts"]),
            last_execution_bar_ts=_timestamps_from_dict(
                raw["last_execution_bar_ts"],
                field="last_execution_bar_ts",
            ),
            execution_bar_filled_quantities={
                str(symbol): float(quantity)
                for symbol, quantity in raw["execution_bar_filled_quantities"].items()
            },
            last_feature_as_of=_to_utc(raw["last_feature_as_of"]),
            last_bar_ts=_timestamps_from_dict(raw["last_bar_ts"], field="last_bar_ts"),
            last_financing_ts=_timestamps_from_dict(
                raw["last_financing_ts"],
                field="last_financing_ts",
            ),
            pending_decision=_pending_decision_from_dict(raw["pending_decision"]),
            pending_ohlcv=[PendingOhlcvDelivery.from_dict(item) for item in raw["pending_ohlcv"]],
            pending_resting_since=_timestamps_from_dict(
                raw["pending_resting_since"], field="pending_resting_since"
            ),
            active_orders=[TrackedOrder.from_dict(item) for item in raw["active_orders"]],
            live_rebalance=(
                LiveRebalance.from_dict(raw["live_rebalance"])
                if raw["live_rebalance"] is not None
                else None
            ),
            equity_peak=float(raw["equity_peak"]),
            prev_equity=float(raw["prev_equity"]),
            status_window_equity=float(raw["status_window_equity"]),
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


# Bounded so an unavailable database cannot grow the checkpoint without limit.
# Sized to cover a long outage for a realistic universe at intraday cadence
# while staying small next to the positions and orders in the same document;
# crossing it is terminal runtime health, not a silent drop.
MAX_PENDING_OHLCV = 2_000


@dataclass(frozen=True, slots=True)
class PendingOhlcvDelivery:
    """One audit row accepted by the runtime but not yet acknowledged.

    The runtime advances its watermark and lands the checkpoint before the
    audit write is attempted, so a failed write used to be lost outright: the
    writer treats an equal row version as an idempotent no-op, and nothing
    re-delivers it. Carrying the pending row in the same checkpoint makes the
    queue land atomically with the watermark it belongs to.

    ``identity`` is the exact subscription, the bar timestamp, and the row
    version. A correction shares its timestamp with the bar it replaces, so
    the version is what makes the two distinct.
    """

    subscription: MarketDataSubscription
    ts: datetime
    available_at: datetime
    bar: dict[str, float]

    @property
    def identity(self) -> tuple[tuple[str, ...], str, str]:
        subscription = self.subscription.to_dict()
        return (
            tuple(subscription[field] for field in sorted(subscription)),
            self.ts.isoformat(),
            self.available_at.isoformat(),
        )

    def to_dict(self) -> dict:
        return {
            "subscription": self.subscription.to_dict(),
            "ts": self.ts.isoformat(),
            "available_at": self.available_at.isoformat(),
            "bar": {key: float(value) for key, value in self.bar.items()},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> PendingOhlcvDelivery:
        ts = _to_utc(raw["ts"])
        available_at = _to_utc(raw["available_at"])
        if ts is None or available_at is None:
            raise ValueError("pending OHLCV delivery requires ts and available_at")
        return cls(
            subscription=MarketDataSubscription.from_dict(raw["subscription"]),
            ts=ts,
            available_at=available_at,
            bar={str(key): float(value) for key, value in raw["bar"].items()},
        )


@dataclass(frozen=True, slots=True)
class HaltResetReadiness:
    """Whether a halted run may start a new risk epoch, and why not.

    Structured rather than a bare exception so an operator and health tooling
    read the same answer, and so the blocking cause survives into an audit
    record instead of only into a traceback.
    """

    ready: bool
    reason: str | None = None
    blocking_symbols: tuple[str, ...] = ()
    required_action: str = ""


class LiveStateStore(Protocol):
    """Minimal persistence boundary used by ``LiveTrader``.

    ``restart_durable`` is the capability an order-capable live run requires:
    whether this store's writes survive the process. The persistence methods
    alone cannot express it — a dictionary satisfies them — so a live runner
    had no way to tell production storage from a test double, and the gap only
    showed after a crash, with the book gone and the broker still holding
    positions. Declaring nothing means not durable: silence is not a claim.
    """

    restart_durable: bool

    def load(self, state_key: str) -> LiveRuntimeState | None: ...

    def save(
        self,
        state: LiveRuntimeState,
        orders: Sequence[TrackedOrder] = (),
    ) -> None: ...

    def acquire_lease(self, state_key: str) -> bool: ...

    def release_lease(self, state_key: str) -> None: ...


class MemoryLiveStateStore:
    """Process-local store for deterministic tests; not restart durability.

    ``restart_durable_for_tests`` exists so a test can exercise the live path
    without a database. It is deliberately verbose and greppable: production
    storage must never acquire durability by accident, and the flag names the
    only reason to set it.
    """

    def __init__(self, *, restart_durable_for_tests: bool = False) -> None:
        self.restart_durable = bool(restart_durable_for_tests)
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
