"""Tests for symbol registry (per-symbol market/data_source/contract economics)."""

from __future__ import annotations

import pytest
from librae.config.symbols import (
    ALLOWED_INSTRUMENT_TYPES,
    SymbolInfo,
    _build_registry,
    get_symbol,
    load_symbol_registry,
    resolve_symbol,
)
from librae.core.run_config import RunConfig


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
        assert btc.data_adapter == "crypto"
        assert btc.venue_symbol == "BTC/USDT"
        assert btc.currency == "USDT"
        assert btc.continuous_alias is False
        assert btc.multiplier == 1.0  # auto-defaulted — not declared in the registry
        assert btc.tick_size is None  # not overridden — CostModel falls back to market_config.py

        assert btc.calendar_id == "24/7"

    def test_txfr1_fields(self, registry):
        txf = registry["TXFR1"]
        assert txf.market == "tw_futures"
        assert txf.data_source == "shioaji"
        assert txf.instrument_type == "contract_monthly"
        assert txf.continuous_alias is True
        assert txf.multiplier == 200.0  # NOT market_config.py's tw_futures default (50, = MXF's)
        assert txf.tick_size == 1.0

        assert txf.calendar_id == "XTAIFEX"

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
                data_adapter="crypto",
                venue_symbol="X/USDT",
                currency="USDT",
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
                data_adapter="crypto",
                venue_symbol="X/USDT",
                currency="USDT",
                tick_size=0.01,
            )

    def test_missing_instrument_type_raises(self):
        with pytest.raises(ValueError, match="instrument_type"):
            _build_registry(
                {
                    "BADSYM": {
                        "market": "crypto",
                        "data_source": "binance_spot",
                        "multiplier": 1.0,
                        "data_adapter": "crypto",
                        "currency": "USDT",
                    }
                }
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
                        "data_adapter": "shioaji",
                        "currency": "TWD",
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
                    "data_adapter": "crypto",
                    "currency": "USDT",
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
                    "data_adapter": "crypto",
                    "currency": "USDT",
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


class TestResolveSymbol:
    @staticmethod
    def _cfg(**overrides) -> RunConfig:
        values = {
            "strategy_name": "x",
            "symbols": ["AAPL"],
            "timeframe": "1d",
            "market": "us_equity",
            "data_source": "ibkr",
            "initial_balance": 100_000.0,
            "mode": "backtest",
        }
        values.update(overrides)
        return RunConfig(**values)

    def test_registered_symbol_uses_venue_metadata(self):
        info = resolve_symbol(
            self._cfg(
                symbols=["BTCUSDT"],
                market="crypto",
                data_source="binance_spot",
            ),
            "BTCUSDT",
        )

        assert info.data_adapter == "crypto"
        assert info.venue_symbol == "BTC/USDT"
        assert info.currency == "USDT"

    def test_unregistered_symbol_uses_explicit_route(self):
        info = resolve_symbol(
            self._cfg(
                instrument_overrides={
                    "AAPL": {
                        "data_adapter": "ibkr",
                        "currency": "USD",
                        "instrument_type": "spot",
                        "security_type": "STK",
                        "exchange": "SMART",
                        "calendar_id": "XNYS",
                    }
                },
                symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
            ),
            "AAPL",
        )

        assert info.market == "us_equity"
        assert info.data_source == "ibkr"
        assert info.data_adapter == "ibkr"
        assert info.security_type == "STK"
        assert info.exchange == "SMART"
        assert info.calendar_id == "XNYS"

    def test_instrument_override_replaces_registered_calendar(self):
        info = resolve_symbol(
            self._cfg(
                symbols=["BTCUSDT"],
                market="crypto",
                data_source="binance_spot",
                instrument_overrides={"BTCUSDT": {"calendar_id": "XNYS"}},
            ),
            "BTCUSDT",
        )

        assert info.calendar_id == "XNYS"

    def test_unknown_data_source_requires_adapter_route(self):
        with pytest.raises(ValueError, match="No data adapter route"):
            resolve_symbol(
                self._cfg(
                    data_source="local",
                    symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
                ),
                "AAPL",
            )

    def test_unregistered_symbol_does_not_infer_product_type(self):
        with pytest.raises(ValueError, match="instrument_type"):
            resolve_symbol(
                self._cfg(
                    data_source="binance_spot",
                    symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
                ),
                "AAPL",
            )

    def test_unregistered_symbol_does_not_infer_currency_from_market(self):
        with pytest.raises(ValueError, match="currency"):
            resolve_symbol(
                self._cfg(
                    data_source="binance_spot",
                    symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
                    instrument_overrides={"AAPL": {"instrument_type": "spot"}},
                ),
                "AAPL",
            )

    def test_ibkr_route_requires_security_type(self):
        with pytest.raises(ValueError, match="security_type"):
            resolve_symbol(
                self._cfg(
                    symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
                    instrument_overrides={
                        "AAPL": {
                            "instrument_type": "spot",
                            "currency": "USD",
                        }
                    },
                ),
                "AAPL",
            )
