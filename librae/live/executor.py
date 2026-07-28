"""Order gateway for simulation and live execution.

Simulation never calls a broker. Live mode submits explicit order requests
and normalizes broker responses into a small execution-report contract.
Portfolio state is updated by ``LiveTrader`` only from confirmed fills.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Literal, Protocol

from librae.core import EPSILON
from librae.core.cost_model import CostModel
from librae.core.strategy import PositionEventType

if TYPE_CHECKING:
    from notifications.telegram import TelegramAdapter

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
    "filled",
    "cancelled",
    "rejected",
]


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
        if not isfinite(self.quantity) or self.quantity <= 0:
            raise ValueError("order quantity must be positive and finite")
        if self.submitted_at.tzinfo is None:
            raise ValueError("submitted_at must be timezone-aware")
        if self.order_type == "limit":
            if self.limit_price is None or not isfinite(self.limit_price) or self.limit_price <= 0:
                raise ValueError("limit orders require a positive finite limit_price")
        elif self.limit_price is not None:
            raise ValueError("market orders cannot have a limit_price")

    def to_signal(self) -> dict:
        signal = {
            "symbol": self.venue_symbol or self.symbol,
            "canonical_symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "client_order_id": self.client_order_id,
            "position_effect": self.position_effect,
        }
        for key in ("security_type", "exchange", "currency"):
            value = getattr(self, key)
            if value is not None:
                signal[key] = value
        if self.limit_price is not None:
            signal["price"] = self.limit_price
        return signal


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
    """Duck-typed gateway implemented by the built-in broker adapters."""

    def prepare_order(self, signal: dict) -> dict: ...

    def place_order(self, signal: dict) -> dict: ...

    def find_order(self, client_order_id: str, symbol: str) -> dict | None: ...

    def get_order(self, order_id: str, symbol: str) -> dict: ...

    def list_open_orders(self, symbol: str) -> list[dict]: ...

    def cancel_order(self, order_id: str, symbol: str) -> dict: ...


class LiveExecutor:
    """Submit live order requests and normalize broker execution reports."""

    def __init__(
        self,
        cost_model: CostModel | Mapping[str, CostModel],
        *,
        simulation: bool = True,
        telegram: TelegramAdapter | None = None,
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
            required = (
                "prepare_order",
                "place_order",
                "find_order",
                "get_order",
                "list_open_orders",
                "cancel_order",
            )
            for symbol, adapter in order_adapters.items():
                missing = [name for name in required if not callable(getattr(adapter, name, None))]
                if missing:
                    raise ValueError(
                        f"Live order_adapter for {symbol!r} is missing lifecycle methods: "
                        + ", ".join(missing)
                    )
        self._cost_models = (
            dict(cost_model) if isinstance(cost_model, Mapping) else {"__default__": cost_model}
        )
        self._simulation = simulation
        self._telegram = telegram
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
    def telegram(self) -> TelegramAdapter | None:
        return self._telegram

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
        client_order_id = (
            f"{self._strategy_name}-{event.symbol}-{event.event_type}-"
            f"{event.ts:%Y%m%dT%H%M%S%f}-{sequence}"
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
            report = self._normalize_report(request, raw)
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
        return self._normalize_report(request, raw) if raw is not None else None

    def get_order(self, request: OrderRequest, order_id: str) -> ExecutionReport:
        """Fetch the latest cumulative state for one broker order."""
        adapter = self.get_order_adapter(request.symbol)
        raw = adapter.get_order(order_id, request.venue_symbol or request.symbol)
        return self._normalize_report(request, raw)

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
        return self._normalize_report(request, raw)

    @classmethod
    def _normalize_report(cls, request: OrderRequest, raw: object) -> ExecutionReport:
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
            "pending_cancel",
            "pendingcancel",
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

    def notify_exit(self, symbol: str, price: float) -> None:
        logger.info("SIGNAL EXIT %s @ %.2f", symbol, price)
        if self._telegram and self._telegram.enabled:
            self._telegram.send_signal(
                strategy=self._strategy_name,
                symbol=symbol,
                side="EXIT",
                price=price,
            )

    def notify_entry(self, symbol: str, side: str, price: float, event_type: str) -> None:
        label = side.upper() if event_type == "open" else f"{side.upper()} ADD"
        logger.info("SIGNAL %s %s @ %.2f", label, symbol, price)
        if self._telegram and self._telegram.enabled:
            self._telegram.send_signal(
                strategy=self._strategy_name,
                symbol=symbol,
                side=label,
                price=price,
            )
