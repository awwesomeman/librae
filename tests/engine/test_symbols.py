"""Tests for symbol registry (per-symbol market/data_source/contract economics)."""

from __future__ import annotations

import pytest
from librae.config.symbols import (
    ALLOWED_INSTRUMENT_TYPES,
    SymbolInfo,
    _build_registry,
    get_symbol,
    load_symbol_registry,
)


@pytest.fixture
def registry() -> dict[str, SymbolInfo]:
    return load_symbol_registry()


class TestLoadSymbolRegistry:
    def test_seeded_symbols_exist(self, registry):
        assert "BTCUSDT" in registry
        assert "TXFR1" in registry

    def test_btcusdt_fields(self, registry):
        btc = registry["BTCUSDT"]
        assert btc.market == "crypto"
        assert btc.data_source == "binance_spot"
        assert btc.instrument_type == "spot"
        assert btc.continuous_alias is False
        assert btc.multiplier == 1.0  # auto-defaulted — not declared in the registry
        assert btc.tick_size is None  # not overridden — CostModel falls back to market_config.py

    def test_txfr1_fields(self, registry):
        txf = registry["TXFR1"]
        assert txf.market == "tw_futures"
        assert txf.data_source == "shioaji"
        assert txf.instrument_type == "contract_monthly"
        assert txf.continuous_alias is True
        assert txf.multiplier == 200.0  # NOT market_config.py's tw_futures default (50, = MXF's)
        assert txf.tick_size == 1.0

    def test_symbol_info_is_frozen(self, registry):
        with pytest.raises(AttributeError):
            registry["BTCUSDT"].market = "tw_futures"


class TestInstrumentTypeValidation:
    def test_invalid_instrument_type_raises(self):
        with pytest.raises(ValueError, match="instrument_type"):
            SymbolInfo(
                symbol="X",
                market="crypto",
                data_source="binance_spot",
                instrument_type="bogus",
                multiplier=1.0,
                tick_size=0.01,
            )

    def test_all_allowed_types_construct_cleanly(self):
        for t in ALLOWED_INSTRUMENT_TYPES:
            SymbolInfo(
                symbol="X",
                market="crypto",
                data_source="binance_spot",
                instrument_type=t,
                multiplier=1.0,
                tick_size=0.01,
            )

    def test_missing_instrument_type_raises(self):
        with pytest.raises(ValueError, match="instrument_type"):
            _build_registry(
                {"BADSYM": {"market": "crypto", "data_source": "binance_spot", "multiplier": 1.0}}
            )


class TestMultiplierTickSizeValidation:
    def test_missing_multiplier_raises_for_contract_types(self):
        with pytest.raises(ValueError, match="multiplier"):
            _build_registry(
                {
                    "BADSYM": {
                        "market": "tw_futures",
                        "data_source": "shioaji",
                        "instrument_type": "contract_monthly",
                        "tick_size": 1.0,
                    }
                }
            )

    def test_missing_multiplier_defaults_to_one_for_spot(self):
        registry = _build_registry(
            {
                "GOODSYM": {
                    "market": "crypto",
                    "data_source": "binance_spot",
                    "instrument_type": "spot",
                }
            }
        )
        assert registry["GOODSYM"].multiplier == 1.0

    def test_missing_tick_size_is_allowed(self):
        registry = _build_registry(
            {
                "GOODSYM": {
                    "market": "crypto",
                    "data_source": "binance_spot",
                    "instrument_type": "spot",
                    "multiplier": 1.0,
                }
            }
        )
        assert registry["GOODSYM"].tick_size is None


class TestGetSymbol:
    def test_get_btcusdt(self):
        info = get_symbol("BTCUSDT")
        assert info.symbol == "BTCUSDT"
        assert info.market == "crypto"

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_symbol("NONEXISTENT")
