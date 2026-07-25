"""Tests for the Telegram notification adapter."""
from __future__ import annotations

import os
from unittest.mock import patch

from notifications.config import NotificationConfig, TelegramConfig
from notifications.telegram import TelegramAdapter, TelegramCredentials


class TestTelegramCredentials:
    def test_from_env(self):
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
        with patch.dict(os.environ, env, clear=True):
            creds = TelegramCredentials.from_env("TELEGRAM")
            assert creds.bot_token == "tok"
            assert creds.chat_id == "123"

    def test_from_env_empty(self):
        with patch.dict(os.environ, {}, clear=True):
            creds = TelegramCredentials.from_env("TELEGRAM")
            assert creds.bot_token == ""
            assert creds.chat_id == ""


class TestTelegramAdapter:
    def _make_adapter(
        self,
        enabled: bool = False,
        signal: bool = True,
        startup: bool = True,
        error: bool = True,
        status_enabled: bool = False,
        bot_token: str = "tok",
        chat_id: str = "123",
    ) -> TelegramAdapter:
        """Helper to build adapter with explicit config."""
        from notifications.config import StatusConfig
        config = TelegramConfig(
            enabled=enabled,
            notifications=NotificationConfig(
                signal=signal, startup=startup, error=error,
                status=StatusConfig(enabled=status_enabled),
            ),
        )
        creds = TelegramCredentials(bot_token=bot_token, chat_id=chat_id)
        return TelegramAdapter(config=config, credentials=creds)

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = TelegramAdapter()
            assert adapter.enabled is False

    def test_enabled_requires_credentials(self):
        config = TelegramConfig(enabled=True)
        creds = TelegramCredentials(bot_token="", chat_id="")
        adapter = TelegramAdapter(config=config, credentials=creds)
        assert adapter.enabled is False

    def test_enabled_with_credentials(self):
        adapter = self._make_adapter(enabled=True)
        assert adapter.enabled is True

    def test_send_text_noop_when_disabled(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.send_text("test") is False

    # --- Notification flag tests ---

    def test_send_signal_noop_when_flag_off(self):
        adapter = self._make_adapter(enabled=True, signal=False)
        assert adapter.send_signal("s", "BTC", "buy", 100.0) is False

    def test_send_alert_noop_when_flag_off(self):
        adapter = self._make_adapter(enabled=True, error=False)
        assert adapter.send_alert("title", "msg") is False

    def test_send_startup_noop_when_flag_off(self):
        adapter = self._make_adapter(enabled=True, startup=False)
        assert adapter.send_startup("s", "BTC", "sim") is False

    def test_send_shutdown_noop_when_flag_off(self):
        adapter = self._make_adapter(enabled=True, startup=False)
        assert adapter.send_shutdown("s", "BTC") is False

    def test_send_status_noop_when_flag_off(self):
        adapter = self._make_adapter(enabled=True, status_enabled=False)
        assert adapter.send_status("s", "BTC", 100_000, -0.05, 500) is False

    # --- Disabled adapter always returns False ---

    def test_send_signal_noop_when_disabled(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.send_signal("test", "BTCUSDT", "buy", 65000.0) is False

    def test_send_startup_noop_when_disabled(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.send_startup("trendpullback", "BTCUSDT", "sim") is False

    def test_send_shutdown_noop_when_disabled(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.send_shutdown("trendpullback", "BTCUSDT") is False

    def test_send_status_noop_when_disabled(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.send_status("s", "BTC", 100_000, -0.05, 500) is False

    # --- chat_id override from config ---

    def test_config_chat_id_overrides_env(self):
        config = TelegramConfig(enabled=True, chat_id="override_id")
        creds = TelegramCredentials(bot_token="tok", chat_id="env_id")
        adapter = TelegramAdapter(config=config, credentials=creds)
        assert adapter._chat_id == "override_id"

    def test_env_chat_id_used_when_config_empty(self):
        config = TelegramConfig(enabled=True, chat_id="")
        creds = TelegramCredentials(bot_token="tok", chat_id="env_id")
        adapter = TelegramAdapter(config=config, credentials=creds)
        assert adapter._chat_id == "env_id"
