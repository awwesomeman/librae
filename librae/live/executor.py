"""LiveExecutor — executor for sim and live modes.

simulation=True (sim): logs signal, sends Telegram. No real orders.
simulation=False (live): mirrors each local fill as a real order via
order_adapter.place_order(). Local position/PnL bookkeeping (process_actions
in core/executor.py) remains authoritative for signal generation — the real
order is best-effort: a broker rejection/error is logged and alerted, not
raised, so the poll loop keeps running. Reconciling actual broker fill price
back into local state is a separate, not-yet-built feature.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from librae.core.cost_model import CostModel
from librae.notifications.telegram import TelegramAdapter

if TYPE_CHECKING:
    from librae.core.executor import OrderEvent

logger = logging.getLogger(__name__)


class OrderAdapter(Protocol):
    """Duck-typed order gateway — matches CryptoAdapter/ShioajiAdapter.place_order()."""

    def place_order(self, signal: dict) -> dict: ...


class LiveExecutor:
    """Executor for sim/live mode.

    Args:
        cost_model: Used for position sizing and simulated fill costs.
        simulation: If True, no real orders are placed.
        telegram: Optional TelegramAdapter for signal notifications.
        strategy_name: Strategy name for Telegram messages.
        order_adapter: Required when simulation=False. Places real orders
            (e.g. ShioajiAdapter, CryptoAdapter with trading credentials).
    """

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
                "of placing real orders (e.g. ShioajiAdapter/CryptoAdapter with "
                "trading credentials)."
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

    def submit_order(self, event: "OrderEvent") -> dict | None:
        """Mirror a local fill as a real order at the broker.

        No-op (returns None) in simulation mode. In live mode, maps the
        position-lifecycle event to a buy/sell order and places it via
        order_adapter. Returns the adapter's result dict, or None if the
        order was rejected/errored (caller should alert — see LiveTrader).
        """
        if self._simulation:
            return None

        is_entry = event.event_type in ("open", "add")
        if event.side == "long":
            side = "buy" if is_entry else "sell"
        else:
            side = "sell" if is_entry else "buy"

        signal = {
            "symbol": event.symbol,
            "side": side,
            "quantity": event.fill_quantity,
            "order_type": "market",
        }
        try:
            result = self._order_adapter.place_order(signal)
            logger.info(
                "Order placed: %s %s (%s) qty=%.4f -> %s",
                side, event.symbol, event.event_type, event.fill_quantity, result,
            )
            return result
        except Exception:
            logger.exception(
                "Order placement FAILED: %s %s (%s) qty=%.4f — "
                "local state may now diverge from broker, reconcile manually",
                side, event.symbol, event.event_type, event.fill_quantity,
            )
            return None

    def notify_exit(self, symbol: str, price: float) -> None:
        """Send exit notification (called by LiveTrader on close action)."""
        logger.info("SIGNAL EXIT %s @ %.2f", symbol, price)
        if self._telegram and self._telegram.enabled:
            self._telegram.send_signal(
                strategy=self._strategy_name,
                symbol=symbol,
                side="EXIT",
                price=price,
            )

