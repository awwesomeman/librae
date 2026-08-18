"""Telegram notification adapter — secrets from env, behavior from YAML config.

Secrets (bot_token, chat_id): loaded via TelegramCredentials.from_env("TELEGRAM").
Behavior (enabled, notification toggles): loaded via TelegramConfig.from_dict().

Usage:
    from librae.notifications.config import TelegramConfig
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

from librae.notifications.config import NotificationConfig, TelegramConfig

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE = 1.0  # seconds
EMOJI_WARNING = "\u26a0\ufe0f"  # ⚠️
EMOJI_SUCCESS = "\u2714\ufe0f"  # ✔️
EMOJI_BUY = "\U0001f7e2"  # 🟢
EMOJI_SELL = "\U0001f534"  # 🔴
RUN_ID_SUFFIX_LEN = 8


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


@dataclass
class TelegramCredentials:
    """Telegram API secrets from environment variables.

    Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
    """

    bot_token: str = ""
    chat_id: str = ""

    @classmethod
    def from_env(cls, prefix: str, **overrides: str) -> TelegramCredentials:
        """Build from env vars ``{prefix}_{FIELD_UPPER}``; overrides win.

        Self-contained on purpose (not librae/brokers/base.py's CredentialConfig)
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
        self._templates = config.templates

        if self._enabled and (not self._token or not self._chat_id):
            logger.warning(
                "Telegram enabled but bot_token or chat_id missing. Disabling notifications."
            )
            self._enabled = False

        self._client: Any = None
        if self._enabled:
            try:
                import httpx

                self._client = httpx.Client(timeout=10)
            except ImportError:
                logger.error(
                    "Telegram notifications require the 'telegram' extra; "
                    "install with pip install 'librae[telegram]'"
                )
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
                logger.exception(
                    "Failed to send Telegram message (attempt %d/%d)", attempt + 1, MAX_RETRIES
                )

            if attempt < MAX_RETRIES - 1:
                time.sleep(BACKOFF_BASE * (2**attempt))

        return False

    def _render(self, key: str, default_lines: list[str], fields: dict[str, Any]) -> str:
        """Render via config.templates[key].format(**fields) if set, else default_lines."""
        template = self._templates.get(key)
        if not template:
            return "\n".join(default_lines)
        try:
            return template.format(**fields)
        except (KeyError, IndexError, ValueError):
            logger.warning("Invalid Telegram template for %r, using default format", key)
            return "\n".join(default_lines)

    def send_signal(
        self,
        strategy: str,
        symbol: str,
        side: str,
        price: float,
        quantity: float | None = None,
        notional: float | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Send an open/add fill notification."""
        if not self._notifications.signal:
            return False
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        side_upper = side.upper()
        direction = "LONG" if "LONG" in side_upper else "SHORT"
        emoji = EMOJI_BUY if direction == "LONG" else EMOJI_SELL
        verb = "added" if "ADD" in side_upper else "opened"
        size_str = ""
        if quantity is not None:
            size_str = f"{quantity:+.4f}" if "ADD" in side_upper else f"{quantity:.4f}"
            if notional is not None:
                size_str += f" (~${notional:,.0f})"
        lines = [
            f"<b>{emoji} {safe_strategy} · {safe_symbol}</b>",
            f"{direction} {verb}",
            f"Fill: <code>{price:,.2f}</code>"
            + (f"   Size: <code>{size_str}</code>" if size_str else ""),
        ]
        if extra:
            for k, v in extra.items():
                lines.append(f"{html.escape(str(k))}: <code>{html.escape(str(v))}</code>")
        lines.append(f"<i>{_timestamp()}</i>")
        text = self._render(
            "signal",
            lines,
            {
                "emoji": emoji,
                "strategy": safe_strategy,
                "symbol": safe_symbol,
                "side": side_upper,
                "direction": direction,
                "verb": verb,
                "price": f"{price:,.2f}",
                "quantity": f"{quantity:.4f}" if quantity is not None else "",
                "notional": f"{notional:,.0f}" if notional is not None else "",
                "timestamp": _timestamp(),
            },
        )
        return self.send_text(text)

    def send_exit(
        self,
        strategy: str,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        net_pnl: float,
        net_return: float,
        periods_held: int,
    ) -> bool:
        """Send a position-close notification with realized P&L."""
        if not self._notifications.signal:
            return False
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        direction = side.upper()
        emoji = EMOJI_BUY if net_pnl >= 0 else EMOJI_SELL
        pnl_str = f"{net_return:+.2%} (${net_pnl:+,.2f})"
        lines = [
            f"<b>{emoji} {safe_strategy} · {safe_symbol}</b>",
            f"{html.escape(direction)} closed  {pnl_str}",
            f"Entry: <code>{entry_price:,.2f}</code> → Exit: <code>{exit_price:,.2f}</code>",
            f"Held: <code>{periods_held}</code> periods",
        ]
        lines.append(f"<i>{_timestamp()}</i>")
        text = self._render(
            "exit",
            lines,
            {
                "emoji": emoji,
                "strategy": safe_strategy,
                "symbol": safe_symbol,
                "side": direction,
                "entry_price": f"{entry_price:,.2f}",
                "exit_price": f"{exit_price:,.2f}",
                "net_pnl": f"{net_pnl:+,.2f}",
                "net_return": f"{net_return:+.2%}",
                "periods_held": str(periods_held),
                "timestamp": _timestamp(),
            },
        )
        return self.send_text(text)

    def send_batch(self, strategy: str, fills: list[dict[str, Any]]) -> bool:
        """Send a digest of many fills from one decision cycle (e.g. a rebalance)."""
        if not self._notifications.signal:
            return False
        safe_strategy = html.escape(strategy)
        lines = [f"<b>{safe_strategy} · rebalance ({len(fills)} fills)</b>"]
        for fill in fills:
            symbol = html.escape(str(fill["symbol"]))
            if fill["type"] == "exit":
                direction = str(fill["side"]).upper()
                net_pnl = float(fill["net_pnl"])
                net_return = float(fill["net_return"])
                emoji = EMOJI_BUY if net_pnl >= 0 else EMOJI_SELL
                lines.append(
                    f"{emoji} {direction} {symbol} closed  {net_return:+.2%} (${net_pnl:+,.2f})"
                )
            else:
                side_upper = str(fill["side"]).upper()
                direction = "LONG" if "LONG" in side_upper else "SHORT"
                emoji = EMOJI_BUY if direction == "LONG" else EMOJI_SELL
                verb = "added" if "ADD" in side_upper else "opened"
                price = float(fill["price"])
                lines.append(f"{emoji} {direction} {verb} {symbol} @ {price:,.2f}")
        lines.append(f"<i>{_timestamp()}</i>")
        return self.send_text("\n".join(lines))

    def send_alert(self, title: str, message: str) -> bool:
        """Send a system alert (e.g. consecutive poll errors)."""
        if not self._notifications.error:
            return False
        safe_title = html.escape(title)
        safe_message = html.escape(message)
        lines = [f"<b>{safe_title}</b>", safe_message, f"<i>{_timestamp()}</i>"]
        text = self._render(
            "alert",
            lines,
            {"title": safe_title, "message": safe_message, "timestamp": _timestamp()},
        )
        return self.send_text(text)

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
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        safe_mode = html.escape(mode)
        run_id_short = (
            ("…" + run_id[-RUN_ID_SUFFIX_LEN:]) if len(run_id) > RUN_ID_SUFFIX_LEN else run_id
        )
        lines = [
            f"<b>[{safe_strategy}] Started</b>",
            f"Symbol: <code>{safe_symbol}</code>",
            f"Mode: <code>{safe_mode}</code>",
        ]
        if run_id_short:
            lines.append(f"Run ID: <code>{html.escape(run_id_short)}</code>")
        lines.append(f"<i>{_timestamp()}</i>")
        text = self._render(
            "startup",
            lines,
            {
                "strategy": safe_strategy,
                "symbol": safe_symbol,
                "mode": safe_mode,
                "run_id": html.escape(run_id_short),
                "timestamp": _timestamp(),
            },
        )
        return self.send_text(text)

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
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        safe_reason = html.escape(reason)
        lines = [
            f"<b>{icon} [{safe_strategy}] Stopped</b>",
            f"Symbol: <code>{safe_symbol}</code>",
            f"Reason: <code>{safe_reason}</code>",
            f"<i>{_timestamp()}</i>",
        ]
        text = self._render(
            "shutdown",
            lines,
            {
                "icon": icon,
                "strategy": safe_strategy,
                "symbol": safe_symbol,
                "reason": safe_reason,
                "timestamp": _timestamp(),
            },
        )
        return self.send_text(text)

    def send_status(
        self,
        strategy: str,
        symbol: str,
        equity: float,
        drawdown: float,
        period_pnl: float,
        num_periods: int,
        position: str = "flat",
    ) -> bool:
        """Send periodic status summary."""
        if not self._notifications.status.enabled:
            return False
        safe_strategy = html.escape(strategy)
        safe_symbol = html.escape(symbol)
        safe_position = html.escape(position)
        equity_str = f"{equity:,.0f}"
        drawdown_str = f"{drawdown:+.2%}"
        period_pnl_str = f"{period_pnl:+,.2f}"
        lines = [
            f"<b>[{safe_strategy}] Status</b>",
            f"Symbol: <code>{safe_symbol}</code>",
            f"Equity: <code>{equity_str}</code>",
            f"Drawdown: <code>{drawdown_str}</code>",
            f"PnL (last {num_periods} periods): <code>{period_pnl_str}</code>",
            f"Position: <code>{safe_position}</code>",
            f"<i>{_timestamp()}</i>",
        ]
        text = self._render(
            "status",
            lines,
            {
                "strategy": safe_strategy,
                "symbol": safe_symbol,
                "equity": equity_str,
                "drawdown": drawdown_str,
                "period_pnl": period_pnl_str,
                "num_periods": str(num_periods),
                "position": safe_position,
                "timestamp": _timestamp(),
            },
        )
        return self.send_text(text)
