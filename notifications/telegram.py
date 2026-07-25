"""Telegram notification adapter — secrets from env, behavior from YAML config.

Secrets (bot_token, chat_id): loaded via TelegramCredentials.from_env("TELEGRAM").
Behavior (enabled, notification toggles): loaded via TelegramConfig.from_dict().

Usage:
    from notifications.config import TelegramConfig
    config = TelegramConfig.from_dict({"enabled": True})
    creds = TelegramCredentials.from_env("TELEGRAM")
    adapter = TelegramAdapter(config=config, credentials=creds)
    adapter.send_signal("strat", "BTCUSDT", "BUY", 65000.0)
"""
from __future__ import annotations

import dataclasses
import html
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from notifications.config import NotificationConfig, TelegramConfig

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds
EMOJI_WARNING = "\u26a0\ufe0f"  # ⚠️
EMOJI_SUCCESS = "\u2714\ufe0f"  # ✔️


@dataclass
class TelegramCredentials:
    """Telegram API secrets from environment variables.

    Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
    """

    bot_token: str = ""
    chat_id: str = ""

    @classmethod
    def from_env(cls, prefix: str, **overrides: str) -> "TelegramCredentials":
        """Build from env vars ``{prefix}_{FIELD_UPPER}``; overrides win.

        Self-contained on purpose (not brokers/base.py's CredentialConfig)
        — a notification adapter has no business depending on brokers.
        """
        kwargs: dict[str, str] = {}
        for f in dataclasses.fields(cls):
            if f.name in overrides:
                kwargs[f.name] = overrides[f.name]
            else:
                env_val = os.environ.get(f"{prefix}_{f.name.upper()}")
                if env_val is not None:
                    kwargs[f.name] = env_val
        return cls(**kwargs)


class TelegramAdapter:
    """Sends messages to Telegram. No-op when disabled or credentials missing."""

    def __init__(
        self,
        config: TelegramConfig | None = None,
        credentials: TelegramCredentials | None = None,
    ) -> None:
        config = config or TelegramConfig()
        creds = credentials or TelegramCredentials.from_env("TELEGRAM")

        # WHY: config.chat_id (from YAML) can override the env-var chat_id,
        # allowing per-strategy routing to different Telegram chats.
        self._token = creds.bot_token
        self._chat_id = config.chat_id or creds.chat_id
        self._enabled = config.enabled
        self._notifications = config.notifications

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but bot_token or chat_id missing. "
                "Disabling notifications."
            )
            self._enabled = False

        self._client: Any = None
        if self._enabled:
            try:
                import httpx
                self._client = httpx.Client(timeout=10)
            except ImportError:
                logger.error("httpx not installed — disabling Telegram notifications")
                self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def notifications(self) -> NotificationConfig:
        return self._notifications

    def send_text(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a plain text message. Returns True if sent successfully."""
        if not self._enabled:
            logger.debug("Telegram disabled, skipping message")
            return False

        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": parse_mode}

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._client.post(url, json=payload)
                if resp.status_code == 200:
                    return True
                if resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", BACKOFF_BASE)
                    logger.warning("Telegram rate-limited, retry after %ss", retry_after)
                    time.sleep(retry_after)
                    continue
                logger.warning("Telegram API error %d: %s", resp.status_code, resp.text)
            except Exception:
                logger.exception("Failed to send Telegram message (attempt %d/%d)", attempt + 1, MAX_RETRIES)

            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2 ** attempt))

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
        if not self._notifications.signal:
            return False
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        lines = [
            f"<b>[{safe_strategy}] {html.escape(side.upper())} {safe_symbol}</b>",
            f"Price: <code>{price:.2f}</code>",
        ]
        if stop is not None:
            lines.append(f"Stop: <code>{stop:.2f}</code>")
        if target is not None:
            lines.append(f"Target: <code>{target:.2f}</code>")
        if extra:
            for k, v in extra.items():
                lines.append(f"{html.escape(str(k))}: <code>{html.escape(str(v))}</code>")
        return self.send_text("\n".join(lines))

    def send_alert(self, title: str, message: str) -> bool:
        """Send a system alert (e.g. consecutive poll errors)."""
        if not self._notifications.error:
            return False
        return self.send_text(f"<b>{html.escape(title)}</b>\n{html.escape(message)}")

    def send_startup(
        self,
        strategy: str,
        symbol: str,
        mode: str,
        run_id: str = "",
    ) -> bool:
        """Send service startup notification."""
        if not self._notifications.startup:
            return False
        lines = [
            f"<b>[{html.escape(strategy)}] Started</b>",
            f"Symbol: <code>{html.escape(symbol)}</code>",
            f"Mode: <code>{html.escape(mode)}</code>",
        ]
        if run_id:
            lines.append(f"Run ID: <code>{html.escape(run_id)}</code>")
        return self.send_text("\n".join(lines))

    def send_shutdown(
        self,
        strategy: str,
        symbol: str,
        reason: str = "normal",
    ) -> bool:
        """Send service shutdown notification."""
        if not self._notifications.startup:
            return False
        icon = EMOJI_WARNING if reason != "normal" else EMOJI_SUCCESS
        lines = [
            f"<b>{icon} [{html.escape(strategy)}] Stopped</b>",
            f"Symbol: <code>{html.escape(symbol)}</code>",
            f"Reason: <code>{html.escape(reason)}</code>",
        ]
        return self.send_text("\n".join(lines))

    def send_status(
        self,
        strategy: str,
        symbol: str,
        equity: float,
        drawdown: float,
        daily_pnl: float,
        position: str = "flat",
    ) -> bool:
        """Send periodic status summary."""
        if not self._notifications.status.enabled:
            return False
        lines = [
            f"<b>[{html.escape(strategy)}] Status</b>",
            f"Symbol: <code>{html.escape(symbol)}</code>",
            f"Equity: <code>{equity:,.2f}</code>",
            f"Drawdown: <code>{drawdown:+.2%}</code>",
            f"Daily PnL: <code>{daily_pnl:+,.2f}</code>",
            f"Position: <code>{html.escape(position)}</code>",
        ]
        return self.send_text("\n".join(lines))

