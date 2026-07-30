from __future__ import annotations

import httpx
import pytest

from brokers.binance_stocks_adapter import (
    BINANCE_STOCKS_API_SCHEMA_VERSION,
    BinanceStocksAdapter,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(
        base_url="https://api.binance.test",
        transport=httpx.MockTransport(handler),
    )


def test_available_symbols_uses_plain_equity_ticker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sapi/v1/equity/market/exchangeInfo"
        assert request.url.params["symbol"] == "MU"
        assert request.headers["X-MBX-APIKEY"] == "test-key"
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "MU",
                        "name": "Micron Technology Inc.",
                        "exchange": "NASDAQ",
                        "tradability": "BUY_SELL",
                    }
                ]
            },
        )

    with _client(handler) as client:
        adapter = BinanceStocksAdapter(api_key="test-key", client=client)
        result = adapter.available_symbols(
            query="mu",
            kind="spot",
            asset_class="equity",
        )

    assert len(result) == 1
    symbol = result[0]
    assert symbol.canonical_symbol == "MU"
    assert symbol.venue_symbol == "MU"
    assert symbol.native_symbol == "MU"
    assert symbol.kind == "spot"
    assert symbol.asset_class == "equity"
    assert symbol.currency == "USD"
    assert symbol.instrument_type == "spot"
    assert symbol.security_type == "STK"
    assert symbol.exchange == "NASDAQ"
    assert symbol.multiplier == 1.0
    assert symbol.market_data_kwargs() == {}
    with pytest.raises(ValueError, match="historical OHLCV"):
        symbol.instrument_override()


def test_available_symbols_rejects_non_matching_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"symbols": [{"symbol": "MUU"}]})

    with _client(handler) as client:
        adapter = BinanceStocksAdapter(api_key="test-key", client=client)
        with pytest.raises(ValueError, match="exact query"):
            adapter.available_symbols(query="MU", kind="spot")


def test_fetch_quote_returns_official_payload_without_guessing_fields() -> None:
    expected = {
        "symbol": "GOOGL",
        "bidPrice": "321.20",
        "askPrice": "321.25",
        "timestamp": 1785376800000,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/sapi/v1/equity/market/quote"
        assert request.url.params["symbol"] == "GOOGL"
        return httpx.Response(200, json=expected)

    with _client(handler) as client:
        adapter = BinanceStocksAdapter(api_key="test-key", client=client)
        assert adapter.fetch_quote("googl") == expected


def test_fetch_quote_empty_body_is_an_explicit_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with _client(handler) as client:
        adapter = BinanceStocksAdapter(api_key="test-key", client=client)
        with pytest.raises(ValueError, match="no quote"):
            adapter.fetch_quote("MU")


def test_missing_api_key_fails_before_network_io() -> None:
    with pytest.raises(ValueError, match="BINANCE_API_KEY"):
        BinanceStocksAdapter()


def test_info_exposes_official_api_schema_version() -> None:
    with _client(lambda request: httpx.Response(200, json={})) as client:
        adapter = BinanceStocksAdapter(api_key="test-key", client=client)
        assert adapter.info().schema_version == BINANCE_STOCKS_API_SCHEMA_VERSION
