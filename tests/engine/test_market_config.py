"""Tests for config.market_config (two-layer MarketConfig architecture)."""
from __future__ import annotations

import pytest
from pathlib import Path

from librae.config.market_config import (
    InstrumentConfig,
    MarketConfig,
    get_instrument,
    load_market_configs,
)

YAML_PATH = Path(__file__).resolve().parents[2] / "librae" / "config" / "markets.yaml"


@pytest.fixture
def instruments() -> dict[str, InstrumentConfig]:
    return load_market_configs(YAML_PATH)


class TestLoadMarketConfigs:
    def test_all_three_instruments_exist(self, instruments):
        assert "BTC_USDT" in instruments
        assert "TW_TXFR" in instruments
        assert "US_SPY" in instruments

    def test_btc_market_is_24h(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.market is not None
        assert btc.market.is_24h is True

    def test_tw_txfr_market_timezone(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.market is not None
        assert txfr.market.timezone == "Asia/Taipei"

    def test_tw_txfr_min_commission(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.min_commission == 100.0

    def test_btc_market_resolved(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.market is not None
        assert btc.market_id == "crypto"
        assert btc.exchange == "binance"
        assert btc.market.quote_currency == "USDT"

    def test_us_spy_market(self, instruments):
        spy = instruments["US_SPY"]
        assert spy.market is not None
        assert spy.exchange == "ib"
        assert spy.market.timezone == "America/New_York"

    def test_btc_exchange_and_data_source(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.exchange == "binance"
        assert btc.data_source == "binance"

    def test_tw_txfr_exchange_and_data_source(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.exchange == "taifex"
        assert txfr.data_source == "shioaji"

    def test_us_spy_exchange_and_data_source(self, instruments):
        spy = instruments["US_SPY"]
        assert spy.exchange == "ib"
        assert spy.data_source == "yfinance"

    def test_market_ids_are_asset_class_level(self, instruments):
        assert instruments["BTC_USDT"].market_id == "crypto"
        assert instruments["TW_TXFR"].market_id == "tw_futures"
        assert instruments["US_SPY"].market_id == "us_equity"

    def test_market_config_has_no_exchange(self, instruments):
        btc = instruments["BTC_USDT"]
        assert not hasattr(btc.market, "exchange")



class TestGetInstrument:
    def test_get_btc(self):
        inst = get_instrument("BTC_USDT", YAML_PATH)
        assert inst.symbol == "BTCUSDT"
        assert inst.market is not None
        assert inst.market_id == "crypto"
        assert inst.exchange == "binance"

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_instrument("NONEXISTENT", YAML_PATH)


