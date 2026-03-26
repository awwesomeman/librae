"""Tests for quant_lab.config.market_config (two-layer MarketConfig architecture)."""
from __future__ import annotations

import pytest
from pathlib import Path

from quant_lab.config.market_config import (
    InstrumentConfig,
    MarketConfig,
    calc_commission,
    calc_slippage,
    get_instrument,
    load_market_configs,
)

YAML_PATH = Path(__file__).resolve().parents[1] / "config" / "markets.yaml"


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
        assert btc.market.exchange == "binance"
        assert btc.market.quote_currency == "USDT"

    def test_us_spy_market(self, instruments):
        spy = instruments["US_SPY"]
        assert spy.market is not None
        assert spy.market.exchange == "ib"
        assert spy.market.timezone == "America/New_York"


class TestCalcCommission:
    def test_btc_commission(self, instruments):
        btc = instruments["BTC_USDT"]
        # rate_based = 68000 * 1.38 * 0.001 = 93.84
        result = calc_commission(btc, price=68000, qty=1.38)
        expected = 68000 * 1.38 * 0.001
        assert abs(result - expected) < 1e-6

    def test_tw_txfr_min_commission(self, instruments):
        txfr = instruments["TW_TXFR"]
        # commission_rate=0.0 → rate_based=0, min_commission=100
        result = calc_commission(txfr, price=20000, qty=1)
        assert result == 100.0

    def test_spy_zero_commission(self, instruments):
        spy = instruments["US_SPY"]
        result = calc_commission(spy, price=500, qty=100)
        assert result == 0.0


class TestCalcSlippage:
    def test_tw_txfr_slippage(self, instruments):
        txfr = instruments["TW_TXFR"]
        # 1 tick * 50.0 TWD/tick * 1 qty = 50.0
        result = calc_slippage(txfr, qty=1)
        assert result == 50.0

    def test_btc_slippage(self, instruments):
        btc = instruments["BTC_USDT"]
        # 2 ticks * 0.01 USD/tick * 1.0 qty = 0.02
        result = calc_slippage(btc, qty=1.0)
        assert abs(result - 0.02) < 1e-10


class TestGetInstrument:
    def test_get_btc(self):
        inst = get_instrument("BTC_USDT", YAML_PATH)
        assert inst.symbol == "BTCUSDT"
        assert inst.market is not None

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_instrument("NONEXISTENT", YAML_PATH)


class TestProperties:
    def test_slippage_cost_property(self, instruments):
        txfr = instruments["TW_TXFR"]
        assert txfr.slippage_cost == 50.0

    def test_commission_per_unit_property(self, instruments):
        btc = instruments["BTC_USDT"]
        assert btc.commission_per_unit == 0.0  # min_commission is 0
