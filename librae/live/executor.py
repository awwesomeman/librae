"""Order gateway for simulation and live execution.

Simulation never calls a broker. Live mode submits explicit order requests
and normalizes broker responses into a small execution-report contract.
Portfolio state is updated by ``LiveTrader`` only from confirmed fills.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Literal, Protocol

from librae.core import EPSILON
from librae.core.cost_model import CostModel

if TYPE_CHECKING:
    from notifications.telegram import TelegramAdapter

    from librae.core.executor import OrderEvent

logger = logging.getLogger(__name__)

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
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

    def __post_init__(self) -> None:
        if not self.client_order_id or not self.symbol:
            raise ValueError("client_order_id and symbol must be non-empty")
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
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "order_type": self.order_type,
            "client_order_id": self.client_order_id,
        }
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

    def place_order(self, signal: dict) -> dict: ...


class LiveExecutor:
    """Submit live order requests and normalize broker execution reports."""

    def __init__(
        self,
        cost_model: CostModel,
        *,
        simulation: bool = True,
        telegram: TelegramAdapter | None = None,
        strategy_name: str = "",
        order_adapter: OrderAdapter | None = None,
    ) -> None:
        if not simulation and order_adapter is None:
            raise ValueError(
                "Live mode (simulation=False) requires an order_adapter capable "
                "of placing real orders."
            )
        self._cost_model = cost_model
        self._simulation = simulation
        self._telegram = telegram
        self._strategy_name = strategy_name
        self._order_adapter = order_adapter

    @property
    def cost_model(self) -> CostModel:
        return self._cost_model

    @property
    def simulation(self) -> bool:
        return self._simulation

    @property
    def telegram(self) -> TelegramAdapter | None:
        return self._telegram

    @property
    def strategy_name(self) -> str:
        return self._strategy_name

    @property
    def order_adapter(self) -> OrderAdapter | None:
        return self._order_adapter

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
        return OrderRequest(
            client_order_id=client_order_id,
            symbol=event.symbol,
            side=side,
            quantity=event.fill_quantity,
            order_type=order_type,
            submitted_at=event.ts,
            reason=event.reason,
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
            raw = self._order_adapter.place_order(request.to_signal())
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
            symbol=request.symbol,
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
            if filled_quantity < requested_quantity - EPSILON:
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
        if status in ("partial", "partially_filled", "partiallyfilled", "filling"):
            return "partial"
        if status in ("open", "new", "accepted", "submitted"):
            return "accepted"
        if status in ("pending", "pending_submit", "pendingsubmit", "presubmitted", ""):
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
