"""Tests for the Telegram notification adapter."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from librae.notifications.config import NotificationConfig, TelegramConfig
from librae.notifications.telegram import TelegramAdapter, TelegramCredentials


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
        from librae.notifications.config import StatusConfig

        config = TelegramConfig(
            enabled=enabled,
            notifications=NotificationConfig(
                signal=signal,
                startup=startup,
                error=error,
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
        assert adapter.send_status("s", "BTC", 100_000, -0.05, 500, 12) is False

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
        assert adapter.send_status("s", "BTC", 100_000, -0.05, 500, 12) is False

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


class TestTokenNeverReachesLogs:
    """The bot token rides in the URL path; neither httpx's per-request INFO
    line nor our own failure logging may write it into container logs."""

    def _enabled_adapter(self) -> TelegramAdapter:
        config = TelegramConfig(enabled=True)
        creds = TelegramCredentials(bot_token="SECRET-TOKEN", chat_id="123")
        return TelegramAdapter(config=config, credentials=creds)

    def test_init_silences_httpx_request_logging(self):
        import logging

        logging.getLogger("httpx").setLevel(logging.NOTSET)
        self._enabled_adapter()
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_send_failure_log_redacts_token(self, caplog):
        adapter = self._enabled_adapter()
        adapter._client = MagicMock()
        adapter._client.post.side_effect = RuntimeError(
            "POST https://api.telegram.org/botSECRET-TOKEN/sendMessage failed"
        )

        with patch("librae.notifications.telegram.time.sleep"), caplog.at_level("WARNING"):
            assert adapter.send_text("hi") is False

        assert caplog.records
        for record in caplog.records:
            assert "SECRET-TOKEN" not in record.getMessage()
        assert any("<bot-token>" in r.getMessage() for r in caplog.records)
