"""Telegram notification adapter with feature flag (on/off).

Feature flag: controlled via TELEGRAM_ENABLED env var or constructor arg.
When disabled, all send operations are no-ops.

Usage:
    adapter = TelegramAdapter()  # reads env vars
    adapter.send_signal("BUY BTCUSDT @ 65000")

    # Or explicitly:
    adapter = TelegramAdapter(enabled=True, bot_token="...", chat_id="...")
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    enabled: bool = field(
        default_factory=lambda: os.environ.get("TELEGRAM_ENABLED", "false").lower() in ("1", "true", "yes")
    )
    bot_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", ""))


class TelegramAdapter:
    """Sends messages to Telegram. No-op when disabled."""

    def __init__(
        self,
        enabled: bool | None = None,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> None:
        cfg = TelegramConfig()
        self._enabled = enabled if enabled is not None else cfg.enabled
        self._token = bot_token or cfg.bot_token
        self._chat_id = chat_id or cfg.chat_id

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing. "
                "Disabling notifications."
            )
            self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a plain text message. Returns True if sent successfully."""
        if not self._enabled:
            logger.debug("Telegram disabled, skipping message")
            return False

        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed, cannot send Telegram message")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = httpx.post(
                url,
                json={"chat_id": self._chat_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
            if resp.status_code == 200:
                return True
            logger.warning("Telegram API error %d: %s", resp.status_code, resp.text)
            return False
        except Exception:
            logger.exception("Failed to send Telegram message")
            return False

    def send_signal(
        self,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        stop: float | None = None,
        target: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Send a formatted trading signal message."""
        lines = [
            f"<b>[{strategy}] {side.upper()} {symbol}</b>",
            f"Price: <code>{price:.2f}</code>",
        ]
        if stop is not None:
            lines.append(f"Stop: <code>{stop:.2f}</code>")
        if target is not None:
            lines.append(f"Target: <code>{target:.2f}</code>")
        if extra:
            for k, v in extra.items():
                lines.append(f"{k}: <code>{v}</code>")
        return self.send_text("\n".join(lines))

    def send_alert(self, title: str, message: str) -> bool:
        """Send a system alert."""
        return self.send_text(f"<b>{title}</b>\n{message}")
