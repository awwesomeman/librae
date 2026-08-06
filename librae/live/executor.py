"""Order gateway for simulation and live execution.

Simulation never calls a broker. Live mode submits explicit order requests
and normalizes broker responses into a small execution-report contract.
Portfolio state is updated by ``LiveTrader`` only from confirmed fills.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from numbers import Real
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

from librae.core import EPSILON
from librae.core.cost_model import CostModel
from librae.core.strategy import PositionEventType, TimeInForce
from librae.core.utils import validate_contract_month

if TYPE_CHECKING:
    from librae.config.symbols import SymbolInfo
    from librae.core.executor import OrderEvent

logger = logging.getLogger(__name__)

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
PositionEffect = PositionEventType
OrderStatus = Literal[
    "submitted",
    "accepted",
    "partial",
    "cancel_pending",
    "filled",
    "cancelled",
    "rejected",
]
REQUIRED_ORDER_ADAPTER_METHODS = (
    "prepare_order",
    "place_order",
    "find_order",
    "get_order",
    "list_open_orders",
    "cancel_order",
    "get_position",
)

# Tightest client_order_id length among librae's live brokers (Binance's
# newClientOrderId): the readable strategy-symbol-event-timestamp-sequence
# id alone already exceeds this for common 6-7 char symbols, regardless of
# strategy_name — see _build_client_order_id.
_MAX_CLIENT_ORDER_ID_LENGTH = 36


def _build_client_order_id(
    strategy_name: str, symbol: str, event_type: str, ts: datetime, sequence: int
) -> str:
    """Build a broker-safe, unique client order id.

    Prefers the readable ``strategy-symbol-event-timestamp-sequence`` form;
    falls back to a shortened id with a content hash suffix when the
    readable form would exceed the tightest broker limit.
    """
    readable = f"{strategy_name}-{symbol}-{event_type}-{ts:%Y%m%dT%H%M%S%f}-{sequence}"
    if len(readable) <= _MAX_CLIENT_ORDER_ID_LENGTH:
        return readable
    digest = hashlib.sha256(readable.encode()).hexdigest()[:10]
    return f"{symbol}-{event_type[:1]}{sequence}-{digest}"[:_MAX_CLIENT_ORDER_ID_LENGTH]


class OrderSignal(TypedDict):
    """Canonical mapping passed from Librae to an order adapter."""

    symbol: str
    canonical_symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType
    client_order_id: str
    position_effect: PositionEffect
    continuous_alias: bool
    reason: NotRequired[str]
    price: NotRequired[float]
    reference_price: NotRequired[float]
    tick_size: NotRequired[float | None]
    security_type: NotRequired[str]
    exchange: NotRequired[str]
    currency: NotRequired[str]
    contract_month: NotRequired[str]
    group_id: NotRequired[str]
    time_in_force: TimeInForce


class BrokerOrderReport(TypedDict, total=False):
    """Cumulative broker order facts accepted by ``LiveExecutor``.

    ``id``/``order_id`` and the snake/camel-case aliases reflect common SDK
    response shapes. A filled quantity requires average price, commission, and
    execution time; ``LiveExecutor`` validates those semantic requirements.
    """

    id: str
    order_id: str
    clientOrderId: str
    client_order_id: str
    symbol: str
    side: str
    status: object
    amount: float
    requested_quantity: float
    filled: float
    filled_quantity: float
    average: float
    average_price: float
    commission: float
    slippage: float
    tax: float
    executed_at: datetime


class BrokerPosition(TypedDict):
    """Canonical current-position snapshot returned by an order adapter."""

    symbol: str
    size: float
    avg_price: float
    unrealized_pnl: float


class BrokerBalance(TypedDict):
    """Canonical cash-currency balance returned by a balance reader."""

    free: float
    used: float
    total: float


@dataclass(frozen=True)
class OrderRequest:
    """One causal broker order created after a completed-bar decision."""

    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType
    submitted_at: datetime
    reason: str = ""
    limit_price: float | None = None
    venue_symbol: str | None = None
    position_effect: PositionEffect = "open"
    security_type: str | None = None
    exchange: str | None = None
    currency: str | None = None
    continuous_alias: bool = False
    contract_month: str | None = None
    group_id: str | None = None
    time_in_force: TimeInForce | None = None

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.symbol:
            raise ValueError("client_order_id and symbol must be non-empty")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"invalid order side: {self.side!r}")
        if self.order_type not in ("market", "limit"):
            raise ValueError(f"invalid order type: {self.order_type!r}")
        if self.position_effect not in ("open", "add", "reduce", "close"):
            raise ValueError(f"invalid position effect: {self.position_effect!r}")
        if self.venue_symbol is not None and not self.venue_symbol:
            raise ValueError("venue_symbol must be non-empty when supplied")
        if not isinstance(self.continuous_alias, bool):
            raise TypeError("continuous_alias must be a bool")
        validate_contract_month(self.contract_month)
        if self.continuous_alias and self.contract_month is not None:
            raise ValueError("continuous_alias and contract_month are mutually exclusive")
        if self.security_type == "FUT" and not (
            self.continuous_alias or self.contract_month is not None
        ):
            raise ValueError("FUT order requires continuous_alias=True or contract_month='YYYYMM'")
        if not isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        if self.submitted_at.tzinfo is None:
            raise ValueError("submitted_at must be timezone-aware")
        if self.order_type == "limit":
            if self.limit_price is None or not isfinite(self.limit_price) or self.limit_price <= 0:
                raise ValueError("limit orders require a positive finite limit_price")
        elif self.limit_price is not None:
            raise ValueError("market orders cannot have a limit_price")
        if self.time_in_force is None:
            # A resting market order is nonsensical everywhere; a limit order
            # rests for the placing bar's duration by default, matching
            # OrderIntent.limit_price's "valid for one eligible bar" contract.
            object.__setattr__(
                self, "time_in_force", "ioc" if self.order_type == "market" else "day"
            )
        elif self.time_in_force not in ("day", "gtc", "ioc", "fok"):
            raise ValueError(f"invalid time_in_force: {self.time_in_force!r}")

    def to_signal(self) -> OrderSignal:
        signal: OrderSignal = {
            "symbol": self.venue_symbol or self.symbol,
            "canonical_symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "client_order_id": self.client_order_id,
            "position_effect": self.position_effect,
            "time_in_force": self.time_in_force,
        }
        for key in ("security_type", "exchange", "currency", "contract_month", "group_id"):
            value = getattr(self, key)
            if value is not None:
                signal[key] = value
        signal["continuous_alias"] = self.continuous_alias
        if self.limit_price is not None:
            signal["price"] = self.limit_price
        return signal


@dataclass(frozen=True)
class PositionRequest:
    """Broker-neutral identity for one configured instrument position.

    The engine always supplies the same canonical fields. Each adapter uses
    only the venue-routing fields it actually needs; broker-specific contract
    resolution remains inside that adapter.
    """

    symbol: str
    venue_symbol: str
    currency: str
    multiplier: float
    security_type: str | None = None
    exchange: str | None = None
    continuous_alias: bool = False
    contract_month: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError("position symbol must be non-empty")
        if not isinstance(self.venue_symbol, str) or not self.venue_symbol:
            raise ValueError("position venue_symbol must be non-empty")
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("position currency must be non-empty")
        if (
            isinstance(self.multiplier, bool)
            or not isinstance(self.multiplier, Real)
            or not isfinite(self.multiplier)
            or self.multiplier <= 0
        ):
            raise ValueError("position multiplier must be positive and finite")
        if self.security_type is not None and (
            not isinstance(self.security_type, str) or not self.security_type
        ):
            raise ValueError("position security_type must be non-empty when supplied")
        if self.exchange is not None and (not isinstance(self.exchange, str) or not self.exchange):
            raise ValueError("position exchange must be non-empty when supplied")
        if not isinstance(self.continuous_alias, bool):
            raise TypeError("position continuous_alias must be a bool")
        validate_contract_month(self.contract_month)
        if self.continuous_alias and self.contract_month is not None:
            raise ValueError("position continuous_alias and contract_month are mutually exclusive")
        if self.security_type == "FUT" and not (
            self.continuous_alias or self.contract_month is not None
        ):
            raise ValueError(
                "FUT position requires continuous_alias=True or contract_month='YYYYMM'"
            )


@dataclass(frozen=True)
class ExecutionReport:
    """Canonical broker order/execution state.

    Costs are denominated in the portfolio cash currency; commission may be
    negative for a broker rebate. A report with ``status="filled"`` or
    ``"partial"`` must carry broker-confirmed quantity, average price, and
    execution timestamp.
    """

    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    status: OrderStatus
    requested_quantity: float
    filled_quantity: float
    average_price: float | None
    commission: float
    slippage: float
    tax: float
    executed_at: datetime | None

    @property
    def has_fill(self) -> bool:
        return self.filled_quantity > EPSILON


class OrderAdapter(Protocol):
    """Required live order lifecycle and position-reconciliation gateway."""

    def prepare_order(self, signal: OrderSignal) -> OrderSignal: ...

    def place_order(self, signal: OrderSignal) -> BrokerOrderReport: ...

    def find_order(
        self,
        client_order_id: str,
        symbol: str,
    ) -> BrokerOrderReport | None: ...

    def get_order(self, order_id: str, symbol: str) -> BrokerOrderReport: ...

    def list_open_orders(self, symbol: str) -> list[BrokerOrderReport]: ...

    def cancel_order(self, order_id: str, symbol: str) -> BrokerOrderReport: ...

    def get_position(self, request: PositionRequest) -> BrokerPosition: ...


class BalanceReader(Protocol):
    """Optional account-balance capability used for live reconciliation."""

    def get_balance(self, currency: str) -> BrokerBalance: ...


class LiveExecutor:
    """Submit live order requests and normalize broker execution reports."""

    def __init__(
        self,
        cost_model: CostModel | Mapping[str, CostModel],
        *,
        simulation: bool = True,
        strategy_name: str = "",
        order_adapter: OrderAdapter | Mapping[str, OrderAdapter] | None = None,
        instruments: Mapping[str, SymbolInfo] | None = None,
    ) -> None:
        if not simulation and order_adapter is None:
            raise ValueError(
                "Live mode (simulation=False) requires an order_adapter capable "
                "of placing real orders."
            )
        order_adapters = (
            dict(order_adapter)
            if isinstance(order_adapter, Mapping)
            else ({"__default__": order_adapter} if order_adapter is not None else {})
        )
        if not simulation:
            for symbol, adapter in order_adapters.items():
                missing = [
                    name
                    for name in REQUIRED_ORDER_ADAPTER_METHODS
                    if not callable(getattr(adapter, name, None))
                ]
                if missing:
                    raise ValueError(
                        f"Live order_adapter for {symbol!r} is missing required methods: "
                        + ", ".join(missing)
                    )
        self._cost_models = (
            dict(cost_model) if isinstance(cost_model, Mapping) else {"__default__": cost_model}
        )
        self._simulation = simulation
        self._strategy_name = strategy_name
        self._order_adapters = order_adapters
        self._instruments = dict(instruments or {})

    def get_cost_model(self, symbol: str) -> CostModel:
        if symbol in self._cost_models:
            return self._cost_models[symbol]
        try:
            return self._cost_models["__default__"]
        except KeyError as exc:
            raise ValueError(f"No cost model configured for {symbol!r}") from exc

    @property
    def simulation(self) -> bool:
        return self._simulation

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    def get_order_adapter(self, symbol: str) -> OrderAdapter | None:
        if self._simulation:
            return None
        if symbol in self._order_adapters:
            return self._order_adapters[symbol]
        try:
            return self._order_adapters["__default__"]
        except KeyError as exc:
            raise ValueError(f"No order adapter configured for {symbol!r}") from exc

    def get_position(self, symbol: str) -> Mapping[str, object]:
        """Read one configured instrument through the shared broker boundary."""
        try:
            instrument = self._instruments[symbol]
        except KeyError as exc:
            raise ValueError(f"No instrument route configured for {symbol!r}") from exc
        request = PositionRequest(
            symbol=symbol,
            venue_symbol=instrument.venue_symbol,
            currency=instrument.currency,
            multiplier=self.get_cost_model(symbol).multiplier,
            security_type=instrument.security_type,
            exchange=instrument.exchange,
            continuous_alias=instrument.continuous_alias,
            contract_month=instrument.contract_month,
        )
        adapter = self.get_order_adapter(symbol)
        raw = adapter.get_position(request)
        if not isinstance(raw, Mapping):
            raise ValueError(f"broker position for {symbol} must be a mapping")
        return raw

    def request_from_event(
        self,
        event: OrderEvent,
        *,
        order_type: OrderType = "market",
        limit_price: float | None = None,
        sequence: int = 0,
    ) -> OrderRequest:
        """Translate a planned lifecycle delta into a broker order request."""
        is_entry = event.event_type in ("open", "add")
        side: OrderSide
        if event.side == "long":
            side = "buy" if is_entry else "sell"
        else:
            side = "sell" if is_entry else "buy"
        client_order_id = _build_client_order_id(
            self._strategy_name, event.symbol, event.event_type, event.ts, sequence
        )
        instrument = self._instruments.get(event.symbol)
        return OrderRequest(
            client_order_id=client_order_id,
            symbol=event.symbol,
            side=side,
            quantity=event.fill_quantity,
            order_type=order_type,
            submitted_at=event.ts,
            reason=event.reason,
            limit_price=limit_price,
            venue_symbol=instrument.venue_symbol if instrument else event.symbol,
            position_effect=event.event_type,
            security_type=instrument.security_type if instrument else None,
            exchange=instrument.exchange if instrument else None,
            currency=instrument.currency if instrument else None,
            continuous_alias=instrument.continuous_alias if instrument else False,
            contract_month=instrument.contract_month if instrument else None,
            group_id=event.group_id,
            time_in_force=event.time_in_force,
        )

    def prepare_order(
        self,
        request: OrderRequest,
        *,
        reference_price: float,
    ) -> OrderRequest:
        """Apply venue quantity/price rules before durable checkpointing."""
        adapter = self.get_order_adapter(request.symbol)
        signal = request.to_signal()
        signal["reference_price"] = reference_price
        instrument = self._instruments.get(request.symbol)
        signal["tick_size"] = instrument.tick_size if instrument else None
        try:
            prepared = adapter.prepare_order(signal)
        except Exception as exc:
            raise ValueError(f"{request.symbol} order preparation failed: {exc}") from exc
        if not isinstance(prepared, dict):
            raise ValueError("prepared order must be a mapping")
        if prepared.get("symbol") != signal["symbol"]:
            raise ValueError("order preparation cannot change venue symbol")

        if prepared.get("quantity") is None:
            raise ValueError("prepared order is missing quantity")
        quantity = float(prepared["quantity"])
        if not isfinite(quantity) or quantity <= 0:
            raise ValueError("prepared order quantity must be finite and positive")
        price_raw = prepared.get("price")
        limit_price = float(price_raw) if price_raw is not None else None
        if request.order_type == "limit" and (
            limit_price is None or not isfinite(limit_price) or limit_price <= 0
        ):
            raise ValueError("prepared limit order requires a finite positive price")
        return replace(
            request,
            quantity=quantity,
            limit_price=limit_price,
        )

    def submit_order(self, request: OrderRequest) -> ExecutionReport | None:
        """Submit a request and return its normalized broker state.

        ``None`` means placement failed or the adapter response violated the
        execution-report contract. Accepted but unfilled orders are returned
        as such; acknowledgement is never treated as a fill.
        """
        if self._simulation:
            return None

        try:
            adapter = self.get_order_adapter(request.symbol)
            raw = adapter.place_order(request.to_signal())
            report = self.normalize_report(request, raw)
        except Exception:
            logger.exception(
                "Order placement/report FAILED: %s %s qty=%.4f; local state unchanged",
                request.side,
                request.symbol,
                request.quantity,
            )
            return None

        logger.info(
            "Order report: %s %s qty=%.4f status=%s filled=%.4f order_id=%s",
            request.side,
            request.symbol,
            request.quantity,
            report.status,
            report.filled_quantity,
            report.order_id,
        )
        return report

    def find_order(self, request: OrderRequest) -> ExecutionReport | None:
        """Find a prior placement by deterministic client id."""
        adapter = self.get_order_adapter(request.symbol)
        raw = adapter.find_order(
            request.client_order_id,
            request.venue_symbol or request.symbol,
        )
        return self.normalize_report(request, raw) if raw is not None else None

    def get_order(self, request: OrderRequest, order_id: str) -> ExecutionReport:
        """Fetch the latest cumulative state for one broker order."""
        adapter = self.get_order_adapter(request.symbol)
        raw = adapter.get_order(order_id, request.venue_symbol or request.symbol)
        return self.normalize_report(request, raw)

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Return raw open orders for startup orphan detection."""
        adapter = self.get_order_adapter(symbol)
        instrument = self._instruments.get(symbol)
        venue_symbol = instrument.venue_symbol if instrument else symbol
        raw = adapter.list_open_orders(venue_symbol)
        if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
            raise ValueError("broker open orders must be a list of mappings")
        return raw

    def cancel_order(self, request: OrderRequest, order_id: str) -> ExecutionReport:
        """Cancel and return the broker's latest cumulative order state."""
        adapter = self.get_order_adapter(request.symbol)
        raw = adapter.cancel_order(order_id, request.venue_symbol or request.symbol)
        return self.normalize_report(request, raw)

    @classmethod
    def normalize_report(cls, request: OrderRequest, raw: object) -> ExecutionReport:
        """Validate and normalize one cumulative broker report."""
        if not isinstance(raw, dict):
            raise ValueError("broker response must be a mapping")

        order_id = str(raw.get("id") or raw.get("order_id") or "")
        status = cls._normalize_status(raw.get("status"))
        requested_quantity = float(
            raw.get("amount") or raw.get("requested_quantity") or request.quantity
        )
        filled_quantity = float(raw.get("filled") or raw.get("filled_quantity") or 0.0)
        average_raw = raw.get("average")
        if average_raw is None:
            average_raw = raw.get("avg_price")
        if average_raw is None and filled_quantity > EPSILON and raw.get("cost") is not None:
            average_raw = float(raw["cost"]) / filled_quantity
        average_price = float(average_raw) if average_raw is not None else None
        commission_raw = cls._extract_commission(
            raw,
            symbol=request.venue_symbol or request.symbol,
            average_price=average_price,
        )
        commission = float(commission_raw or 0.0)
        slippage = float(raw.get("slippage") or 0.0)
        tax = float(raw.get("tax") or 0.0)
        executed_at = cls._parse_timestamp(
            raw.get("executed_at")
            or raw.get("lastTradeTimestamp")
            or raw.get("last_trade_timestamp")
        )

        if filled_quantity > requested_quantity + EPSILON:
            raise ValueError("broker filled quantity exceeds requested quantity")
        if min(requested_quantity, filled_quantity, slippage, tax) < 0:
            raise ValueError("broker report quantities, slippage, and tax must be non-negative")
        if not all(
            isfinite(value)
            for value in (requested_quantity, filled_quantity, commission, slippage, tax)
        ):
            raise ValueError("broker report quantities and costs must be finite")

        if filled_quantity > EPSILON:
            if average_price is None or not isfinite(average_price) or average_price <= 0:
                raise ValueError("filled report is missing a positive average price")
            if executed_at is None:
                raise ValueError("filled report is missing broker execution timestamp")
            if commission_raw is None:
                raise ValueError("filled report is missing broker-confirmed commission/fee")
            if filled_quantity < requested_quantity - EPSILON and status not in (
                "cancelled",
                "rejected",
            ):
                status = "partial"
            elif status not in ("cancelled", "rejected"):
                status = "filled"
        elif status in ("partial", "filled"):
            raise ValueError(f"{status} report has no filled quantity")

        if status not in ("rejected",) and not order_id:
            raise ValueError("broker report is missing order id")

        return ExecutionReport(
            order_id=order_id,
            client_order_id=str(raw.get("clientOrderId") or request.client_order_id),
            symbol=request.symbol,
            side=request.side,
            status=status,
            requested_quantity=requested_quantity,
            filled_quantity=filled_quantity,
            average_price=average_price,
            commission=commission,
            slippage=slippage,
            tax=tax,
            executed_at=executed_at,
        )

    @staticmethod
    def _normalize_status(raw_status: object) -> OrderStatus:
        status = str(raw_status or "").lower().replace("-", "_").replace(" ", "_")
        if any(token in status for token in ("reject", "fail", "error", "inactive")):
            return "rejected"
        if status in ("pending_cancel", "pendingcancel"):
            return "cancel_pending"
        if "cancel" in status or "expired" in status:
            return "cancelled"
        if status in ("filled", "closed", "complete", "completed"):
            return "filled"
        if status in (
            "partial",
            "part_filled",
            "partfilled",
            "partially_filled",
            "partiallyfilled",
            "filling",
        ):
            return "partial"
        if status in ("open", "new", "accepted", "submitted"):
            return "accepted"
        if status in (
            "pending",
            "pending_submit",
            "pendingsubmit",
            "api_pending",
            "apipending",
            "presubmitted",
            "",
        ):
            return "submitted"
        raise ValueError(f"unsupported broker order status: {raw_status!r}")

    @classmethod
    def _extract_commission(
        cls,
        raw: dict,
        *,
        symbol: str,
        average_price: float | None,
    ) -> float | None:
        if raw.get("commission") is not None:
            return float(raw["commission"])
        fees = raw.get("fees")
        if isinstance(fees, list) and fees:
            costs = [
                cls._fee_in_cash(fee, symbol=symbol, average_price=average_price)
                for fee in fees
                if isinstance(fee, dict) and fee.get("cost") is not None
            ]
            if costs:
                return sum(costs)
        fee = raw.get("fee")
        if isinstance(fee, dict):
            return cls._fee_in_cash(fee, symbol=symbol, average_price=average_price)
        if fee is not None:
            return float(fee)
        return None

    @staticmethod
    def _fee_in_cash(
        fee: dict,
        *,
        symbol: str,
        average_price: float | None,
    ) -> float | None:
        cost = fee.get("cost")
        if cost is None:
            return None
        value = float(cost)
        currency = str(fee.get("currency") or "").upper()
        if not currency or "/" not in symbol:
            return value

        base, quote = symbol.upper().split("/", 1)
        quote = quote.split(":", 1)[0]
        if currency == quote:
            return value
        if currency == base and average_price is not None:
            return value * average_price
        raise ValueError(
            f"cannot convert {currency} execution fee to {quote}; "
            "adapter must return cash-denominated commission"
        )

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        if isinstance(value, (int, float)):
            seconds = float(value) / 1000.0 if abs(float(value)) >= 1e11 else float(value)
            return datetime.fromtimestamp(seconds, tz=UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
