"""Tests for the Telegram notification adapter."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from brokers.telegram import TelegramAdapter, TelegramConfig


class TestTelegramConfig:
    def test_defaults_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = TelegramConfig()
            assert cfg.enabled is False
            assert cfg.bot_token == ""
            assert cfg.chat_id == ""

    def test_enabled_from_env(self):
        env = {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}
        with patch.dict(os.environ, env, clear=True):
            cfg = TelegramConfig()
            assert cfg.enabled is True
            assert cfg.bot_token == "tok"
            assert cfg.chat_id == "123"


class TestTelegramAdapter:
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            adapter = TelegramAdapter()
            assert adapter.enabled is False

    def test_send_text_noop_when_disabled(self):
        adapter = TelegramAdapter(enabled=False)
        result = adapter.send_text("test message")
        assert result is False

    def test_send_signal_noop_when_disabled(self):
        adapter = TelegramAdapter(enabled=False)
        result = adapter.send_signal("test", "BTCUSDT", "buy", 65000.0)
        assert result is False

    def test_explicit_enable_requires_credentials(self):
        adapter = TelegramAdapter(enabled=True, bot_token="", chat_id="")
        assert adapter.enabled is False  # auto-disabled due to missing credentials

    def test_explicit_enable_with_credentials(self):
        adapter = TelegramAdapter(enabled=True, bot_token="tok", chat_id="123")
        assert adapter.enabled is True

    def test_send_alert_noop_when_disabled(self):
        adapter = TelegramAdapter(enabled=False)
        result = adapter.send_alert("Alert", "Something happened")
        assert result is False
