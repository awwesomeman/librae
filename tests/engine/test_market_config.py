"""Tests for market config (single-file, market-level).

multiplier/tick_size are deliberately NOT covered here — they're
per-symbol (see test_symbols.py), not market-level; MarketConfig doesn't
carry them (see librae/config/market_config.py's module docstring for why).
"""
from __future__ import annotations

import pytest

from librae.config.market_config import (
    MarketConfig,
    get_market,
    load_market_configs,
)


@pytest.fixture
def markets() -> dict[str, MarketConfig]:
    return load_market_configs()


class TestLoadMarketConfigs:
    def test_all_three_markets_exist(self, markets):
        assert "crypto" in markets
        assert "tw_futures" in markets
        assert "us_equity" in markets

    def test_crypto_fields(self, markets):
        crypto = markets["crypto"]
        assert crypto.commission_rate == 0.001
        assert crypto.long_margin_rate == 1.0
        assert crypto.short_margin_rate == 1.0

    def test_tw_futures_fields(self, markets):
        tw = markets["tw_futures"]
        assert tw.min_commission == 100.0
        assert tw.tax_rate == 0.00002
        assert tw.long_margin_rate == 0.075
        assert tw.short_margin_rate == 0.075

    def test_us_equity_fields(self, markets):
        us = markets["us_equity"]
        assert us.long_margin_rate == 1.0
        assert us.short_margin_rate == 0.5

    def test_market_config_is_frozen(self, markets):
        with pytest.raises(AttributeError):
            markets["crypto"].commission_rate = 0.5


class TestGetMarket:
    def test_get_crypto(self):
        market = get_market("crypto")
        assert market.name == "crypto"
        assert market.commission_rate == 0.001

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_market("NONEXISTENT")
