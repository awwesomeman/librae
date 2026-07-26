"""LiveExecutor — executor for sim and live modes.

simulation=True (sim): logs signal, sends Telegram. No real orders.
simulation=False (live): mirrors each local fill as a real order via
order_adapter.place_order(). Local position/PnL bookkeeping (process_actions
in core/executor.py) remains authoritative for signal generation — the real
order is best-effort: a broker rejection/error is logged and alerted, not
raised, so the poll loop keeps running. LiveTrader._reconcile_fill polls the
broker's position after each fill and alerts on mismatch (never overwrites
local state — see librae/live/engine.py).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from librae.core.cost_model import CostModel

if TYPE_CHECKING:
    from notifications.telegram import TelegramAdapter

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

    @property
    def order_adapter(self) -> OrderAdapter | None:
        """None in simulation mode. In live mode, the same adapter instance
        used to place orders — exposed so callers can also read back
        broker-side state via its duck-typed ``get_position()`` (e.g. for
        startup reconciliation)."""
        return self._order_adapter

    def submit_order(self, event: OrderEvent) -> dict | None:
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

        # Client-supplied order ID: lets the broker's own audit trail (and,
        # for adapters that support it, broker-side dedup) tie an order back
        # to the exact local fill event that triggered it. Deterministic
        # from the event itself, so resubmitting the same event always
        # produces the same ID.
        client_order_id = (
            f"{self._strategy_name}-{event.symbol}-{event.event_type}-{event.ts:%Y%m%dT%H%M%S}"
        )
        signal = {
            "symbol": event.symbol,
            "side": side,
            "quantity": event.fill_quantity,
            "order_type": "market",
            "client_order_id": client_order_id,
        }
        try:
            result = self._order_adapter.place_order(signal)
            logger.info(
                "Order placed: %s %s (%s) qty=%.4f -> %s",
                side,
                event.symbol,
                event.event_type,
                event.fill_quantity,
                result,
            )
            return result
        except Exception:
            logger.exception(
                "Order placement FAILED: %s %s (%s) qty=%.4f — "
                "local state may now diverge from broker, reconcile manually",
                side,
                event.symbol,
                event.event_type,
                event.fill_quantity,
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

    def notify_entry(self, symbol: str, side: str, price: float, event_type: str) -> None:
        """Send entry/add notification (called by LiveTrader on open/add
        events) — symmetric with notify_exit, so an operator watching
        Telegram sees when a position opens, not only when it closes."""
        label = side.upper() if event_type == "open" else f"{side.upper()} ADD"
        logger.info("SIGNAL %s %s @ %.2f", label, symbol, price)
        if self._telegram and self._telegram.enabled:
            self._telegram.send_signal(
                strategy=self._strategy_name,
                symbol=symbol,
                side=label,
                price=price,
            )
