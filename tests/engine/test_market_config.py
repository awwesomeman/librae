"""Tests for market config (per-market YAML with defaults inheritance)."""
from __future__ import annotations

import pytest
from pathlib import Path

from librae.config.market_config import (
    InstrumentConfig,
    MarketConfig,
    get_instrument,
    load_market_configs,
)

MARKETS_DIR = Path(__file__).resolve().parents[2] / "librae" / "config" / "markets"


@pytest.fixture
def instruments() -> dict[str, InstrumentConfig]:
    return load_market_configs(MARKETS_DIR)


class TestLoadMarketConfigs:
    def test_all_three_instruments_exist(self, instruments):
        assert "BTC_USDT" in instruments
        assert "TW_TXFR" in instruments
        assert "US_SPY" in instruments

    def test_btc_symbol(self, instruments):
        assert instruments["BTC_USDT"].symbol == "BTCUSDT"

    def test_btc_market_resolved(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.market is not None
        assert btc.market.asset_class == "crypto"
        assert btc.market.quote_currency == "USDT"

    def test_tw_txfr_market(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.market.asset_class == "futures"
        assert txfr.market.timezone == "Asia/Taipei"

    def test_us_spy_market(self, instruments):
        spy = instruments["US_SPY"]
        assert spy.market.asset_class == "equity"
        assert spy.market.timezone == "America/New_York"

    def test_market_ids(self, instruments):
        assert instruments["BTC_USDT"].market_id == "crypto"
        assert instruments["TW_TXFR"].market_id == "tw_futures"
        assert instruments["US_SPY"].market_id == "us_equity"

    def test_exchange(self, instruments):
        assert instruments["BTC_USDT"].exchange == "binance"
        assert instruments["TW_TXFR"].exchange == "taifex"
        assert instruments["US_SPY"].exchange == "ib"


class TestDefaultsInheritance:
    def test_btc_inherits_crypto_defaults(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.commission_rate == 0.001
        assert btc.min_commission == 0.0
        assert btc.transaction_tax == 0.0
        assert btc.slippage_ticks == 2

    def test_tw_txfr_inherits_futures_defaults(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.min_commission == 100.0
        assert txfr.commission_rate == 0.0

    def test_instrument_overrides_default(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.tick_size == 1.0
        assert txfr.tick_value == 50.0


class TestMultiplierProperty:
    def test_spot_multiplier_is_one(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.multiplier == pytest.approx(1.0)

    def test_futures_multiplier(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.multiplier == pytest.approx(50.0)


class TestGetInstrument:
    def test_get_btc(self):
        inst = get_instrument("BTC_USDT", MARKETS_DIR)
        assert inst.symbol == "BTCUSDT"
        assert inst.market is not None

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_instrument("NONEXISTENT", MARKETS_DIR)
