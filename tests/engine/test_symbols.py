"""Tests for symbol registry (per-symbol market/data_source/contract economics)."""

from __future__ import annotations

import pytest
from librae.config.symbols import (
    ALLOWED_INSTRUMENT_TYPES,
    AvailableSymbol,
    SymbolInfo,
    _build_registry,
    available_symbols,
    canonicalize_price_to_increment,
    get_symbol,
    load_symbol_registry,
    resolve_symbol,
)
from librae.core.run_config import AccountConfig, RunConfig
from librae.core.strategy import OrderIntent


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


class TestAvailableSymbol:
    def test_exact_future_exposes_fetch_and_run_config_routing(self):
        symbol = AvailableSymbol(
            broker="ibkr",
            canonical_symbol="MNQ_202609",
            venue_symbol="MNQ",
            native_symbol="MNQU6",
            name="Micro E-mini Nasdaq-100",
            kind="future",
            asset_class="index",
            currency="USD",
            instrument_type="contract_quarterly",
            security_type="FUT",
            exchange="CME",
            contract_month="202609",
            delivery_month="202609",
            contract_rank=0,
            multiplier=2.0,
            tick_size=0.25,
            price_increment=0.25,
            min_notional=5.0,
        )

        assert symbol.market_data_kwargs() == {
            "continuous_alias": False,
            "contract_month": "202609",
            "security_type": "FUT",
            "exchange": "CME",
            "currency": "USD",
        }
        assert symbol.instrument_override() == {
            "broker": "ibkr",
            "data_adapter": "ibkr",
            "venue_symbol": "MNQ",
            "currency": "USD",
            "instrument_type": "contract_quarterly",
            "continuous_alias": False,
            "security_type": "FUT",
            "exchange": "CME",
            "contract_month": "202609",
            "price_increment": 0.25,
            "min_notional": 5.0,
        }
        assert symbol.cost_override() == {
            "multiplier": 2.0,
            "tick_size": 0.25,
        }

    def test_dispatcher_forwards_filters_to_injected_adapter(self):
        expected = (
            AvailableSymbol(
                broker="binance",
                canonical_symbol="MUUSDT_PERP",
                venue_symbol="MU/USDT:USDT",
                native_symbol="MUUSDT",
                name="MUUSDT",
                kind="perpetual",
                asset_class="equity",
                currency="USDT",
                instrument_type="contract_perpetual",
                multiplier=1.0,
            ),
        )

        class Adapter:
            def available_symbols(self, **kwargs):
                assert kwargs == {
                    "query": "MU",
                    "kind": "perpetual",
                    "asset_class": "equity",
                }
                return expected

        result = available_symbols(
            "binance",
            query="MU",
            kind="perpetual",
            asset_class="equity",
            adapter=Adapter(),
        )

        assert result == expected

    def test_dispatcher_routes_binance_stock_filters_to_injected_adapter(self):
        expected = (
            AvailableSymbol(
                broker="binance",
                canonical_symbol="MU",
                venue_symbol="MU",
                native_symbol="MU",
                name="Micron Technology Inc.",
                kind="spot",
                asset_class="equity",
                currency="USD",
                instrument_type="spot",
                security_type="STK",
                exchange="NASDAQ",
                multiplier=1.0,
            ),
        )

        class Adapter:
            def available_symbols(self, **kwargs):
                assert kwargs == {
                    "query": "MU",
                    "kind": "spot",
                    "asset_class": "equity",
                }
                return expected

        result = available_symbols(
            "binance",
            query="MU",
            kind="spot",
            asset_class="equity",
            adapter=Adapter(),
        )

        assert result == expected

    def test_binance_requires_explicit_product_dimensions(self):
        with pytest.raises(ValueError, match="requires kind"):
            available_symbols("binance", query="BTCUSDT", asset_class="crypto")
        with pytest.raises(ValueError, match="requires asset_class"):
            available_symbols("binance", query="BTCUSDT", kind="spot")


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
                contract_month=(
                    "202609" if t.startswith("contract_") and t != "contract_perpetual" else None
                ),
                tick_size=0.01,
            )

    def test_dated_contract_requires_exact_month_or_continuous_alias(self):
        common = {
            "symbol": "ES_202609",
            "market": "us_futures",
            "data_source": "ibkr",
            "instrument_type": "contract_quarterly",
            "multiplier": 50.0,
            "data_adapter": "ibkr",
            "venue_symbol": "ES",
            "currency": "USD",
        }

        exact = SymbolInfo(**common, contract_month="202609")
        rolling = SymbolInfo(**(common | {"symbol": "ES_FRONT"}), continuous_alias=True)

        assert exact.contract_month == "202609"
        assert rolling.continuous_alias is True
        with pytest.raises(ValueError, match="exactly one"):
            SymbolInfo(**common)
        with pytest.raises(ValueError, match="exactly one"):
            SymbolInfo(**common, continuous_alias=True, contract_month="202609")

    @pytest.mark.parametrize("contract_month", ["20269", "202600", "202613", "SEP2026"])
    def test_invalid_contract_month_raises(self, contract_month):
        with pytest.raises(ValueError, match="YYYYMM"):
            SymbolInfo(
                symbol="ES_BAD",
                market="us_futures",
                data_source="ibkr",
                instrument_type="contract_quarterly",
                multiplier=50.0,
                data_adapter="ibkr",
                venue_symbol="ES",
                currency="USD",
                contract_month=contract_month,
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
            "mode": "backtest",
        }
        values.update(overrides)
        symbol = values["symbols"][0]
        route = (values.get("instrument_overrides") or {}).get(symbol, {})
        currency = route.get("currency")
        if currency is None:
            try:
                currency = get_symbol(symbol).currency
            except KeyError:
                currency = "USD"
        values["account"] = AccountConfig(currency=currency, initial_cash=100_000.0)
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

    def test_unregistered_symbol_accepts_external_adapter_name(self):
        info = resolve_symbol(
            self._cfg(
                data_source="vendor_feed",
                instrument_overrides={
                    "AAPL": {
                        "data_adapter": "vendor_plugin",
                        "currency": "USD",
                        "instrument_type": "spot",
                    }
                },
                symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
            ),
            "AAPL",
        )

        assert info.data_adapter == "vendor_plugin"

    def test_unregistered_symbol_falls_back_to_run_wide_calendar_id(self):
        info = resolve_symbol(
            self._cfg(
                calendar_id="XTAI",
                instrument_overrides={
                    "AAPL": {
                        "data_adapter": "ibkr",
                        "currency": "USD",
                        "instrument_type": "spot",
                        "security_type": "STK",
                        "exchange": "SMART",
                    }
                },
                symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
            ),
            "AAPL",
        )

        assert info.calendar_id == "XTAI"

    def test_per_symbol_calendar_id_wins_over_run_wide_default(self):
        info = resolve_symbol(
            self._cfg(
                calendar_id="XTAI",
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

    def test_instrument_override_rejects_unknown_key_with_symbol(self):
        with pytest.raises(ValueError, match=r"AAPL.*price_incremnt"):
            self._cfg(
                instrument_overrides={
                    "AAPL": {"price_incremnt": 0.01},
                }
            )

    def test_instrument_override_rejects_non_string_field_name_cleanly(self):
        with pytest.raises(ValueError, match=r"AAPL.*123"):
            self._cfg(instrument_overrides={"AAPL": {123: "invalid"}})

    def test_instrument_override_rejects_non_mapping_route(self):
        with pytest.raises(TypeError, match=r"AAPL.*mapping"):
            RunConfig(
                strategy_name="x",
                symbols=["AAPL"],
                timeframe="1d",
                market="us_equity",
                data_source="ibkr",
                account=AccountConfig(currency="USD", initial_cash=10_000.0),
                mode="backtest",
                instrument_overrides={"AAPL": []},
            )

    def test_instrument_override_rejects_unconfigured_symbol(self):
        with pytest.raises(ValueError, match="unconfigured symbol 'GHOST'"):
            self._cfg(instrument_overrides={"GHOST": {"calendar_id": "XNYS"}})

    @pytest.mark.parametrize(
        ("field_name", "value"),
        [
            ("price_increment", 0.0),
            ("price_increment", float("nan")),
            ("min_notional", True),
            ("quantity_step", "0.1"),
        ],
    )
    def test_instrument_override_rejects_invalid_execution_values(
        self,
        field_name,
        value,
    ):
        with pytest.raises(ValueError, match=field_name):
            self._cfg(instrument_overrides={"AAPL": {field_name: value}})

    def test_instrument_override_rejects_conflicting_contract_selection(self):
        with pytest.raises(ValueError, match="cannot set both"):
            self._cfg(
                instrument_overrides={
                    "AAPL": {
                        "continuous_alias": True,
                        "contract_month": "202609",
                    }
                }
            )

    def test_resolved_execution_constraints_stay_separate_from_cost_tick(self):
        info = resolve_symbol(
            self._cfg(
                instrument_overrides={
                    "AAPL": {
                        "instrument_type": "spot",
                        "currency": "USD",
                        "security_type": "STK",
                        "price_increment": 0.25,
                        "min_notional": 100.0,
                    }
                },
                symbol_cost_overrides={"AAPL": {"multiplier": 1.0}},
            ),
            "AAPL",
        )

        assert info.tick_size is None
        assert info.price_increment == 0.25
        assert info.min_notional == 100.0

    def test_exact_ibkr_future_keeps_canonical_symbol_and_explicit_month(self):
        info = resolve_symbol(
            self._cfg(
                symbols=["ES_202609"],
                market="us_futures",
                data_source="ibkr",
                instrument_overrides={
                    "ES_202609": {
                        "data_adapter": "ibkr",
                        "currency": "USD",
                        "instrument_type": "contract_quarterly",
                        "security_type": "FUT",
                        "exchange": "CME",
                        "venue_symbol": "ES",
                        "contract_month": "202609",
                    }
                },
                symbol_cost_overrides={"ES_202609": {"multiplier": 50.0}},
            ),
            "ES_202609",
        )

        assert info.symbol == "ES_202609"
        assert info.venue_symbol == "ES"
        assert info.contract_month == "202609"
        assert info.continuous_alias is False

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

    def test_ibkr_execution_route_requires_security_type_from_non_ibkr_data(self):
        with pytest.raises(ValueError, match="security_type"):
            resolve_symbol(
                self._cfg(
                    symbols=["BTCUSDT"],
                    market="crypto",
                    data_source="binance_spot",
                    broker="ibkr",
                ),
                "BTCUSDT",
            )


class TestExecutablePriceIncrement:
    @staticmethod
    def _instrument(price_increment: float | None, *, tick_size: float | None = None):
        return SymbolInfo(
            symbol="TEST",
            market="test",
            data_source="test",
            instrument_type="spot",
            multiplier=1.0,
            data_adapter="test",
            venue_symbol="TEST",
            currency="USD",
            tick_size=tick_size,
            price_increment=price_increment,
        )

    @pytest.mark.parametrize(
        ("increment", "price"),
        [
            (0.01, 100.13),
            (0.25, 100.25),
            (0.0001, 0.1234),
            (0.1, 0.1 + 0.2),
        ],
    )
    def test_accepts_equity_futures_crypto_and_float_boundaries(self, increment, price):
        instrument = self._instrument(increment)

        instrument.validate_order_prices(
            OrderIntent(
                action="long",
                limit_price=price,
                stop_price=price,
                take_profit_price=price,
            )
        )

    def test_canonicalizes_float_representation_to_declared_grid(self):
        assert canonicalize_price_to_increment(0.1 + 0.2, 0.1) == 0.3

    @pytest.mark.parametrize(
        "field_name",
        ["limit_price", "stop_price", "take_profit_price"],
    )
    def test_rejects_half_tick(self, field_name):
        instrument = self._instrument(0.25)

        with pytest.raises(ValueError, match=rf"{field_name}.*price_increment"):
            instrument.validate_order_prices(OrderIntent(action="long", **{field_name: 100.125}))

    def test_cost_tick_is_not_an_authoritative_price_grid(self):
        instrument = self._instrument(None, tick_size=0.25)

        instrument.validate_order_prices(OrderIntent(action="long", limit_price=100.13))
