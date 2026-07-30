"""Typed configuration for Telegram notifications.

Behavior settings (enabled, which notifications to send) come from YAML config.
Secrets (bot_token, chat_id) come from environment variables via TelegramCredentials.from_env().

Usage:
    config = TelegramConfig.from_dict(yaml_dict.get("telegram", {}))
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StatusConfig:
    """Periodic status summary settings."""

    enabled: bool = False
    interval_periods: int = 12


@dataclass
class NotificationConfig:
    """Per-notification-type toggles."""

    signal: bool = True
    startup: bool = True
    error: bool = True
    status: StatusConfig = field(default_factory=StatusConfig)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> NotificationConfig:
        """Build from YAML dict. Missing keys use dataclass defaults."""
        status_raw = d.get("status", {})
        if not isinstance(status_raw, dict):
            status_raw = {}
        return cls(
            signal=bool(d.get("signal", cls.signal)),
            startup=bool(d.get("startup", cls.startup)),
            error=bool(d.get("error", cls.error)),
            status=StatusConfig(
                enabled=bool(status_raw.get("enabled", StatusConfig.enabled)),
                interval_periods=int(
                    status_raw.get("interval_periods", StatusConfig.interval_periods)
                ),
            ),
        )


@dataclass
class TelegramConfig:
    """Top-level Telegram behavior config (from YAML, not env vars)."""

    enabled: bool = False
    chat_id: str = ""
    notifications: NotificationConfig = field(default_factory=NotificationConfig)

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> TelegramConfig:
        """Build from YAML dict. Missing keys use dataclass defaults."""
        notif_raw = d.get("notifications", {})
        if not isinstance(notif_raw, dict):
            notif_raw = {}
        return cls(
            enabled=bool(d.get("enabled", cls.enabled)),
            chat_id=str(d.get("chat_id", cls.chat_id) or ""),
            notifications=NotificationConfig.from_dict(notif_raw),
        )
