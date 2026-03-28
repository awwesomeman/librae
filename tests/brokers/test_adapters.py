from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from brokers.base import (
    AdapterInfo,
    CredentialConfig,
    MarketDataAdapter,
    OrderSide,
    OrderStatus,
    OrderType,
)
from brokers.binance import (
    BinanceAccountAdapter,
    BinanceMarketDataAdapter,
    BinanceOrderAdapter,
)
from brokers.shioaji import (
    ShioajiAccountAdapter,
    ShioajiMarketDataAdapter,
    ShioajiOrderAdapter,
)
from brokers.wiring import build_adapter_bundle

NOW = datetime(2026, 3, 6, 12, 0, 0, tzinfo=timezone.utc)


def test_adapter_info_defaults() -> None:
    info = AdapterInfo(adapter_id="test", venue="TEST", market_type="spot")
    assert info.schema_version == "1.0.0"


def test_binance_and_shioaji_info() -> None:
    assert BinanceMarketDataAdapter().info().venue == "BINANCE"
    assert ShioajiMarketDataAdapter().info().venue == "SHIOAJI"


@pytest.mark.asyncio
async def test_market_data_fetch_bars_runs() -> None:
    md = BinanceMarketDataAdapter(market_type="paper")
    bars = await md.fetch_bars("BTCUSDT", "H1", NOW, NOW.replace(hour=15))
    assert len(bars) >= 1
    assert bars[0].symbol == "BTCUSDT"


@pytest.mark.asyncio
async def test_order_submit_and_get_order_runs() -> None:
    od = ShioajiOrderAdapter(market_type="backtest")
    got_fills = []
    await od.subscribe_fills(lambda f: got_fills.append(f))
    order = await od.submit_order("MXFR1", OrderSide.BUY, OrderType.MARKET, 1.0, "contracts")
    assert order.status == OrderStatus.FILLED
    fetched = await od.get_order(order.order_id)
    assert fetched.order_id == order.order_id
    assert len(got_fills) == 1


@pytest.mark.asyncio
async def test_account_balance_and_positions_runs() -> None:
    ac = BinanceAccountAdapter(market_type="paper")
    bal = await ac.get_balance()
    pos = await ac.get_positions()
    assert "USDT" in bal
    assert len(pos) == 1


def test_wiring_builds_bundle() -> None:
    b = build_adapter_bundle("binance", "paper")
    assert isinstance(b.market_data, MarketDataAdapter)
    s = build_adapter_bundle("shioaji", "backtest")
    assert s.order.info().venue == "SHIOAJI"
    with pytest.raises(ValueError):
        build_adapter_bundle("unknown")


# ---------------------------------------------------------------------------
# Connection state & async context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connected_state_tracks_connect_disconnect() -> None:
    md = BinanceMarketDataAdapter()
    assert md.connected is False
    await md.connect()
    assert md.connected is True
    await md.disconnect()
    assert md.connected is False


@pytest.mark.asyncio
async def test_async_context_manager() -> None:
    async with BinanceOrderAdapter() as od:
        assert od.connected is True
        order = await od.submit_order(
            "BTCUSDT", OrderSide.BUY, OrderType.MARKET, 0.1, "asset",
        )
        assert order.status == OrderStatus.FILLED
    assert od.connected is False


@pytest.mark.asyncio
async def test_account_context_manager() -> None:
    async with BinanceAccountAdapter() as ac:
        assert ac.connected is True
    assert ac.connected is False


# ---------------------------------------------------------------------------
# CredentialConfig.from_env
# ---------------------------------------------------------------------------


@dataclass
class _TestCreds(CredentialConfig):
    api_key: str = ""
    secret: str = ""


def test_credential_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", "k123")
    monkeypatch.setenv("TEST_SECRET", "s456")
    creds = _TestCreds.from_env("TEST")
    assert creds.api_key == "k123"
    assert creds.secret == "s456"


def test_credential_config_override_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("TEST_API_KEY", "from_env")
    creds = _TestCreds.from_env("TEST", api_key="explicit")
    assert creds.api_key == "explicit"


# ---------------------------------------------------------------------------
# CryptoAdapter.info()
# ---------------------------------------------------------------------------


def test_crypto_adapter_info() -> None:
    from brokers.crypto_adapter import CryptoAdapter

    adapter = CryptoAdapter.__new__(CryptoAdapter)
    adapter._exchange = None
    adapter._read_only = True
    adapter._exchange_id = "binance"
    info = adapter.info()
    assert info.venue == "BINANCE"
    assert info.adapter_id == "crypto_binance"
