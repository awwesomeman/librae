"""Tests for symbol registry (per-symbol market + data_source mapping)."""
from __future__ import annotations

import pytest

from librae.config.symbols import (
    ALLOWED_INSTRUMENT_TYPES,
    SymbolInfo,
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
        assert btc.multiplier is None  # no override — uses markets.yaml's crypto default

    def test_txfr1_fields(self, registry):
        txf = registry["TXFR1"]
        assert txf.market == "tw_futures"
        assert txf.data_source == "shioaji"
        assert txf.instrument_type == "contract_monthly"
        assert txf.continuous_alias is True
        assert txf.multiplier == 200.0  # NOT markets.yaml's tw_futures default (50, = MXF's)

    def test_symbol_info_is_frozen(self, registry):
        with pytest.raises(AttributeError):
            registry["BTCUSDT"].market = "tw_futures"


class TestInstrumentTypeValidation:
    def test_invalid_instrument_type_raises(self):
        with pytest.raises(ValueError, match="instrument_type"):
            SymbolInfo(symbol="X", market="crypto", data_source="binance_spot", instrument_type="bogus")

    def test_all_allowed_types_construct_cleanly(self):
        for t in ALLOWED_INSTRUMENT_TYPES:
            SymbolInfo(symbol="X", market="crypto", data_source="binance_spot", instrument_type=t)

    def test_missing_instrument_type_in_yaml_raises(self, tmp_path):
        bad_yaml = tmp_path / "symbols.yaml"
        bad_yaml.write_text("BADSYM:\n  market: crypto\n  data_source: binance_spot\n")
        with pytest.raises(ValueError, match="instrument_type"):
            load_symbol_registry(bad_yaml)


class TestGetSymbol:
    def test_get_btcusdt(self):
        info = get_symbol("BTCUSDT")
        assert info.symbol == "BTCUSDT"
        assert info.market == "crypto"

    def test_get_missing_raises(self):
        with pytest.raises(KeyError, match="NONEXISTENT"):
            get_symbol("NONEXISTENT")
