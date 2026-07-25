"""Tests for NotificationConfig and TelegramConfig dataclasses."""
from __future__ import annotations

from notifications.config import NotificationConfig, StatusConfig, TelegramConfig


class TestStatusConfig:
    def test_defaults(self):
        cfg = StatusConfig()
        assert cfg.enabled is False
        assert cfg.interval_periods == 12


class TestNotificationConfig:
    def test_defaults(self):
        cfg = NotificationConfig()
        assert cfg.signal is True
        assert cfg.startup is True
        assert cfg.error is True
        assert cfg.status.enabled is False

    def test_from_dict_empty(self):
        cfg = NotificationConfig.from_dict({})
        assert cfg.signal is True
        assert cfg.status.interval_periods == 12

    def test_from_dict_partial_override(self):
        cfg = NotificationConfig.from_dict({"signal": False})
        assert cfg.signal is False
        assert cfg.startup is True  # unchanged

    def test_from_dict_nested_status(self):
        cfg = NotificationConfig.from_dict({
            "status": {"enabled": True, "interval_periods": 6},
        })
        assert cfg.status.enabled is True
        assert cfg.status.interval_periods == 6
        assert cfg.signal is True  # unchanged


class TestTelegramConfig:
    def test_defaults(self):
        cfg = TelegramConfig()
        assert cfg.enabled is False
        assert cfg.chat_id == ""
        assert cfg.notifications.signal is True

    def test_from_dict_empty(self):
        cfg = TelegramConfig.from_dict({})
        assert cfg.enabled is False

    def test_from_dict_enabled_with_notifications(self):
        cfg = TelegramConfig.from_dict({
            "enabled": True,
            "chat_id": "12345",
            "notifications": {
                "signal": False,
                "status": {"enabled": True},
            },
        })
        assert cfg.enabled is True
        assert cfg.chat_id == "12345"
        assert cfg.notifications.signal is False
        assert cfg.notifications.startup is True  # default
        assert cfg.notifications.status.enabled is True
        assert cfg.notifications.status.interval_periods == 12  # default

    def test_from_dict_none_chat_id_becomes_empty(self):
        cfg = TelegramConfig.from_dict({"chat_id": None})
        assert cfg.chat_id == ""
