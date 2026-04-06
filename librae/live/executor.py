"""LiveExecutor — executor for sim and live modes.

simulation=True (sim): logs signal, sends Telegram. No real orders.
simulation=False (live): reserved for Phase 4 (raises NotImplementedError).
"""
from __future__ import annotations

import logging

from librae.core.cost_model import CostModel
from librae.core.strategy import Action
from librae.notifications.telegram import TelegramAdapter

logger = logging.getLogger(__name__)


class LiveExecutor:
    """Executor for sim/live mode.

    Args:
        cost_model: Used for position sizing and simulated fill costs.
        simulation: If True, no real orders are placed.
        telegram: Optional TelegramAdapter for signal notifications.
        strategy_name: Strategy name for Telegram messages.
    """

    def __init__(
        self,
        cost_model: CostModel,
        *,
        simulation: bool = True,
        telegram: TelegramAdapter | None = None,
        strategy_name: str = "",
    ) -> None:
        self._cost_model = cost_model
        self._simulation = simulation
        self._telegram = telegram
        self._strategy_name = strategy_name

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

    def _notify_signal(self, action: Action, price: float) -> None:
        """Send entry notification via Telegram."""
        side = "BUY" if action.type == "buy" else "SELL"
        logger.info(
            "SIGNAL %s %s @ %.2f (reason: %s)",
            side, action.symbol, price, action.reason or "n/a",
        )
        if self._telegram and self._telegram.enabled:
            self._telegram.send_signal(
                strategy=self._strategy_name,
                symbol=action.symbol,
                side=side,
                price=price,
            )
