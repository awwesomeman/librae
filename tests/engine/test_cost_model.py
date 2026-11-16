"""Tests for CostModel: spot vs futures PnL, commission, slippage, tax."""

from __future__ import annotations

import numpy as np
import pytest
from librae.config.market_config import get_market
from librae.core.cost_model import CostModel

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def crypto_cost() -> CostModel:
    """BTC_USDT: spot, multiplier=1, no tax."""
    return CostModel(
        multiplier=1.0,
        commission_rate=0.001,
        min_commission=0.0,
        slippage_ticks=2.0,
        tick_size=0.01,
        tax_rate=0.0,
    )


@pytest.fixture
def tw_futures_cost() -> CostModel:
    """Generic TW futures cost math fixture (multiplier=50 is an arbitrary
    test value here, not TXF's real multiplier — see TestFromConfig below
    for the real per-symbol multiplier resolution, which TXFR1 needs)."""
    return CostModel(
        multiplier=50.0,
        commission_rate=0.0,
        min_commission=100.0,
        slippage_ticks=1.0,
        tick_size=1.0,
        tax_rate=0.00002,
    )


# ── CostModel.from_market ─────────────────────────────────────────────


class TestFromMarket:
    """multiplier is a required kwarg here — from_market() does not get
    it from MarketConfig (see librae/config/symbols.py). tick_size is
    optional and falls back to the market's own value when omitted."""

    def test_crypto_multiplier_is_one(self) -> None:
        market = get_market("crypto")
        cm = CostModel.from_market(market, multiplier=1.0, tick_size=0.01)
        assert np.isclose(cm.multiplier, 1.0)
        assert np.isclose(cm.commission_rate, 0.001)
        assert np.isclose(cm.tax_rate, 0.0)

    def test_futures_multiplier(self) -> None:
        market = get_market("tw_futures")
        cm = CostModel.from_market(market, multiplier=200.0, tick_size=1.0)
        assert np.isclose(cm.multiplier, 200.0)
        assert np.isclose(cm.min_commission, 0.0)
        assert cm.commission_per_contract == 0.0  # per-symbol only, never from the market

    def test_tick_size_omitted_falls_back_to_market_default(self) -> None:
        market = get_market("crypto")
        cm = CostModel.from_market(market, multiplier=1.0)
        assert np.isclose(cm.tick_size, 0.01)

    def test_futures_margin_rate(self) -> None:
        market = get_market("tw_futures")
        cm = CostModel.from_market(market, multiplier=200.0, tick_size=1.0)
        assert np.isclose(cm.long_margin_rate, 0.075)
        assert np.isclose(cm.short_margin_rate, 0.075)

    def test_us_equity_margin_rate(self) -> None:
        market = get_market("us_equity")
        cm = CostModel.from_market(market, multiplier=1.0, tick_size=0.01)
        assert np.isclose(cm.long_margin_rate, 1.0)
        assert np.isclose(cm.short_margin_rate, 0.5)


# ── PnL calculation ──────────────────────────────────────────────────────


class TestCalcPnl:
    def test_spot_pnl(self, crypto_cost: CostModel) -> None:
        # Buy at 50000, sell at 51000, qty=0.5
        pnl = crypto_cost.calc_pnl(50_000.0, 51_000.0, 0.5)
        assert np.isclose(pnl, 500.0)  # (51000-50000) * 0.5 * 1.0

    def test_futures_pnl(self, tw_futures_cost: CostModel) -> None:
        # Buy at 20000, sell at 20050, qty=2 contracts
        pnl = tw_futures_cost.calc_pnl(20_000.0, 20_050.0, 2.0)
        # (20050-20000) * 2 * 50 = 5000
        assert np.isclose(pnl, 5000.0)

    def test_losing_trade(self, crypto_cost: CostModel) -> None:
        pnl = crypto_cost.calc_pnl(50_000.0, 49_000.0, 1.0)
        assert np.isclose(pnl, -1000.0)


# ── Commission ────────────────────────────────────────────────────────────


class TestCommission:
    def test_crypto_rate_based(self, crypto_cost: CostModel) -> None:
        # 50000 * 0.5 * 1.0 * 0.001 = 25
        comm = crypto_cost.calc_commission(50_000.0, 0.5)
        assert np.isclose(comm, 25.0)

    def test_futures_min_commission(self, tw_futures_cost: CostModel) -> None:
        # rate=0 → rate_based=0, but min_commission=100
        comm = tw_futures_cost.calc_commission(20_000.0, 1.0)
        assert np.isclose(comm, 100.0)


# ── Slippage ──────────────────────────────────────────────────────────────


class TestSlippage:
    def test_crypto_slippage(self, crypto_cost: CostModel) -> None:
        # 2 ticks * 0.01 * 0.5 * 1.0 = 0.01
        slip = crypto_cost.calc_slippage(0.5)
        assert np.isclose(slip, 0.01)

    def test_futures_slippage(self, tw_futures_cost: CostModel) -> None:
        # 1 tick * 1.0 * 2 contracts * 50 = 100
        slip = tw_futures_cost.calc_slippage(2.0)
        assert np.isclose(slip, 100.0)


class TestDynamicSlippage:
    """bar_volume/volume_impact_ticks default to disabled — every pre-existing
    calc_slippage(qty) call site above is unaffected."""

    def test_bar_volume_omitted_unaffected(self, crypto_cost: CostModel) -> None:
        assert crypto_cost.calc_slippage(0.5) == crypto_cost.calc_slippage(0.5, bar_volume=None)

    def test_zero_volume_impact_ticks_unaffected_even_with_bar_volume(
        self, crypto_cost: CostModel
    ) -> None:
        # crypto_cost fixture has volume_impact_ticks=0.0 (dataclass default)
        assert crypto_cost.calc_slippage(0.5) == crypto_cost.calc_slippage(0.5, bar_volume=10.0)

    def test_impact_scales_with_participation(self) -> None:
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            volume_impact_ticks=10.0,
        )
        # 10% participation (qty=10 / volume=100) -> +1 impact tick -> 2 ticks total
        slip_10pct = cm.calc_slippage(10.0, bar_volume=100.0)
        assert np.isclose(slip_10pct, 2.0 * 0.01 * 10.0 * 1.0)

        # 50% participation -> +5 impact ticks -> 6 ticks total, strictly worse
        slip_50pct = cm.calc_slippage(50.0, bar_volume=100.0)
        assert np.isclose(slip_50pct / 50.0, 6.0 * 0.01)
        assert slip_50pct / 50.0 > slip_10pct / 10.0  # higher participation -> worse per-unit cost

    def test_zero_or_missing_bar_volume_skips_impact(self) -> None:
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            volume_impact_ticks=10.0,
        )
        base = cm.calc_slippage(10.0)
        assert cm.calc_slippage(10.0, bar_volume=0.0) == base
        assert cm.calc_slippage(10.0, bar_volume=None) == base


# ── Liquidation ───────────────────────────────────────────────────────────


class TestLiquidationPrice:
    """maintenance_margin_rate defaults to 0 (disabled) — every fixture
    above (crypto_cost/tw_futures_cost) leaves it unset, so this needs its
    own leveraged fixture."""

    @pytest.fixture
    def leveraged_cost(self) -> CostModel:
        return CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
            long_margin_rate=0.1,
            short_margin_rate=0.1,
            maintenance_margin_rate=0.05,
            long_margin_mode="dynamic",
            short_margin_mode="dynamic",
        )

    def test_long_formula(self, leveraged_cost: CostModel) -> None:
        # entry*(1 + maintenance - margin) = 100*(1+0.05-0.1) = 95
        assert leveraged_cost.liquidation_price(100.0, "long") == pytest.approx(95.0)

    def test_short_formula(self, leveraged_cost: CostModel) -> None:
        # entry*(1 - maintenance + margin) = 100*(1-0.05+0.1) = 105
        assert leveraged_cost.liquidation_price(100.0, "short") == pytest.approx(105.0)

    def test_disabled_when_maintenance_margin_rate_zero(self, crypto_cost: CostModel) -> None:
        assert crypto_cost.liquidation_price(100.0, "long") is None

    def test_disabled_when_margin_rate_leaves_no_buffer(self) -> None:
        # margin_rate <= maintenance_margin_rate: already under-margined at
        # entry, not a sane liquidation price to compute — fail safe.
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
            long_margin_rate=0.05,
            short_margin_rate=0.05,
            maintenance_margin_rate=0.05,
            long_margin_mode="dynamic",
            short_margin_mode="dynamic",
        )
        assert cm.liquidation_price(100.0, "long") is None


# ── Tax ───────────────────────────────────────────────────────────────────


class TestTax:
    def test_crypto_no_tax(self, crypto_cost: CostModel) -> None:
        tax = crypto_cost.calc_tax(50_000.0, 1.0)
        assert np.isclose(tax, 0.0)

    def test_futures_tax_symmetric(self, tw_futures_cost: CostModel) -> None:
        # 20000 * 1 * 50 * 0.00002 = 20.0 — same for buy and sell
        tax = tw_futures_cost.calc_tax(20_000.0, 1.0)
        assert np.isclose(tax, 20.0)


# ── Total cost ────────────────────────────────────────────────────────────


class TestTotalCost:
    def test_crypto_cost(self, crypto_cost: CostModel) -> None:
        # commission=25 + slippage=0.01 + tax=0
        cost = crypto_cost.total_cost(50_000.0, 0.5)
        assert np.isclose(cost, 25.01)

    def test_futures_cost(self, tw_futures_cost: CostModel) -> None:
        # commission=100 + slippage=50 + tax=20
        cost = tw_futures_cost.total_cost(20_000.0, 1.0)
        assert np.isclose(cost, 170.0)


# ── CostModel.from_config: per-symbol multiplier resolution ──────────────
# from_config() must consult the symbol registry, or market_config.py's
# tw_futures multiplier (50, matching MXF) is silently applied to TXFR1 (real
# multiplier 200).


class TestFromConfig:
    def _cfg(self, symbol: str, market: str = "tw_futures", data_source: str = "shioaji", **kwargs):
        from librae.config.symbols import get_symbol
        from librae.core.run_config import AccountConfig, RunConfig

        try:
            currency = get_symbol(symbol).currency
        except KeyError:
            currency = "USD"
        return RunConfig(
            strategy_name="x",
            symbols=[symbol],
            timeframe="5m",
            market=market,
            data_source=data_source,
            account=AccountConfig(currency=currency, initial_cash=100_000.0),
            mode="backtest",
            **kwargs,
        )

    def test_registered_symbol_uses_its_own_multiplier(self) -> None:
        cm = CostModel.from_config(self._cfg("TXFR1"))
        assert cm.multiplier == 200.0  # not market_config.py's tw_futures default (50)

    def test_unregistered_symbol_raises_without_cost_overrides(self) -> None:
        with pytest.raises(ValueError, match="multiplier"):
            CostModel.from_config(self._cfg("SOME_UNREGISTERED_SYMBOL"))

    def test_unregistered_symbol_works_with_explicit_multiplier_override(self) -> None:
        """tick_size isn't required even for an unregistered symbol — it
        falls back to the market default."""
        cm = CostModel.from_config(
            self._cfg(
                "ZEF202612", cost_overrides={"multiplier": 10.0, "commission_per_contract": 0.0}
            ),
        )
        assert cm.multiplier == 10.0
        assert cm.tick_size == 1.0  # tw_futures market default

    def test_crypto_spot_unaffected(self) -> None:
        cm = CostModel.from_config(
            self._cfg("BTCUSDT", market="crypto", data_source="binance_spot")
        )
        assert cm.multiplier == 1.0

    def test_spot_symbol_without_explicit_multiplier_defaults_to_one(self) -> None:
        """BTCUSDT doesn't declare multiplier in the registry at all — spot
        auto-defaults, no per-symbol registration needed."""
        from librae.config.symbols import get_symbol

        assert get_symbol("BTCUSDT").multiplier == 1.0

    def test_spot_symbol_tick_size_falls_back_to_market_default(self) -> None:
        cm = CostModel.from_config(
            self._cfg("BTCUSDT", market="crypto", data_source="binance_spot")
        )
        assert cm.tick_size == 0.01  # crypto market default — BTCUSDT doesn't override it

    def test_explicit_cost_overrides_win_over_symbol_multiplier(self) -> None:
        cfg = self._cfg(
            "BTCUSDT_QUARTERLY",
            market="crypto",
            data_source="binance_futures_continuous",
            cost_overrides={"multiplier": 999.0},
        )
        assert CostModel.from_config(cfg).multiplier == 999.0

    def test_explicit_override_object_wins_over_everything(self) -> None:
        explicit = CostModel.zero()
        cm = CostModel.from_config(self._cfg("TXFR1"), override=explicit)
        assert cm is explicit

    def test_symbol_param_resolves_a_different_symbol_than_cfg_symbol(self) -> None:
        """Multi-asset run: cfg.symbol is symbols[0] (TXFR1), but a caller
        can resolve any other symbol in the run via symbol=."""
        cfg = self._cfg("TXFR1")
        cm = CostModel.from_config(cfg, symbol="MXFR1")
        assert cm.multiplier == 50.0

    def test_symbol_param_resolves_its_own_market_costs(self) -> None:
        """A mixed-market run must not apply cfg.market or the first
        symbol's commission/margin schedule to every asset."""
        from librae.core.run_config import AccountConfig, RunConfig

        cfg = RunConfig(
            strategy_name="x",
            symbols=["TXFR1", "MU"],
            timeframe="1d",
            market="multi",
            data_source="multi",
            account=AccountConfig(currency="USD", initial_cash=100_000.0),
            mode="backtest",
        )

        futures = CostModel.from_config(cfg, symbol="TXFR1")
        equity = CostModel.from_config(cfg, symbol="MU")

        assert futures.commission_per_contract == 40.0
        assert futures.long_margin_rate == 0.075
        assert equity.min_commission == 0.0
        assert equity.commission_per_contract == 0.0
        assert equity.long_margin_rate == 1.0
        assert equity.short_margin_rate == 0.5

    def test_symbol_cost_overrides_unregistered_symbol_without_touching_symbols_yaml(self) -> None:
        cm = CostModel.from_config(
            self._cfg(
                "MY_CUSTOM_SYMBOL",
                market="crypto",
                data_source="x",
                symbol_cost_overrides={"MY_CUSTOM_SYMBOL": {"multiplier": 1.0}},
            )
        )
        assert cm.multiplier == 1.0

    def test_symbol_cost_overrides_wins_over_run_wide_cost_overrides(self) -> None:
        cfg = self._cfg(
            "BTCUSDT_QUARTERLY",
            market="crypto",
            data_source="binance_futures_continuous",
            cost_overrides={"multiplier": 111.0},
            symbol_cost_overrides={"BTCUSDT_QUARTERLY": {"multiplier": 222.0}},
        )
        assert CostModel.from_config(cfg).multiplier == 222.0

    def test_cost_overrides_still_applies_as_fallback_for_symbols_not_listed(self) -> None:
        """cost_overrides is the run-wide fallback; symbol_cost_overrides only
        overrides the specific symbols it lists."""
        cfg = self._cfg(
            "BTCUSDT_QUARTERLY",
            market="crypto",
            data_source="binance_futures_continuous",
            cost_overrides={"multiplier": 111.0},
            symbol_cost_overrides={"MXFR1": {"multiplier": 222.0}},
        )
        assert CostModel.from_config(cfg, symbol="BTCUSDT_QUARTERLY").multiplier == 111.0

    @pytest.mark.parametrize(
        ("overrides", "field"),
        [
            ({"symbol_cost_overrides": {"TXFR1": {"multiplier": 50.0}}}, "multiplier"),
            ({"cost_overrides": {"tick_size": 0.5}}, "tick_size"),
            ({"instrument_overrides": {"TXFR1": {"calendar_id": "24/7"}}}, "calendar_id"),
        ],
    )
    def test_an_override_cannot_change_a_product_spec(self, overrides, field: str) -> None:
        with pytest.raises(ValueError, match=rf"set {field}=.* for 'TXFR1', whose product 'TX'"):
            self._cfg("TXFR1", **overrides)

    def test_an_override_may_restate_a_product_spec_or_set_its_own_fee(self) -> None:
        cfg = self._cfg(
            "TXFR1",
            symbol_cost_overrides={"TXFR1": {"multiplier": 200.0, "commission_per_contract": 25.0}},
        )
        cm = CostModel.from_config(cfg)
        assert (cm.multiplier, cm.commission_per_contract) == (200.0, 25.0)


class TestRunWideContractEconomics:
    """A run-wide multiplier or tick_size in a multi-symbol run must not
    replace a registered symbol's own value: TXFR1 (200) and MXFR1 (50)
    cannot both be worth 10 per point."""

    @staticmethod
    def _cfg(symbols, **kwargs):
        from librae.core.run_config import AccountConfig, RunConfig

        return RunConfig(
            strategy_name="x",
            symbols=list(symbols),
            timeframe="5m",
            market="tw_futures",
            data_source="shioaji",
            account=AccountConfig(currency="TWD", initial_cash=1_000_000.0),
            mode="backtest",
            **kwargs,
        )

    @pytest.mark.parametrize(
        ("field", "value", "registered"),
        [("multiplier", 10.0, "200.0"), ("tick_size", 0.5, "1.0")],
    )
    def test_value_that_differs_from_a_registered_one_is_refused(
        self, field: str, value: float, registered: str
    ) -> None:
        with pytest.raises(
            ValueError,
            match=rf"set {field}={value} for 'TXFR1', whose product 'TX' has {field}={registered}",
        ):
            self._cfg(["TXFR1", "MXFR1"], cost_overrides={field: value})

    def test_symbol_that_sets_its_own_value_is_exempt(self) -> None:
        cfg = self._cfg(
            ["TXFR1", "MXFR1"],
            cost_overrides={"multiplier": 10.0},
            symbol_cost_overrides={
                "TXFR1": {"multiplier": 200.0},
                "MXFR1": {"multiplier": 50.0},
            },
        )

        assert CostModel.from_config(cfg, symbol="MXFR1").multiplier == 50.0

    def test_value_equal_to_every_registered_one_is_accepted(self) -> None:
        cfg = self._cfg(["TXFR1", "MXFR1"], cost_overrides={"tick_size": 1.0})

        assert CostModel.from_config(cfg, symbol="MXFR1").tick_size == 1.0

    def test_registered_symbol_without_its_own_tick_size_takes_the_run_wide_one(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        cfg = RunConfig(
            strategy_name="x",
            symbols=["BTCUSDT", "ETHUSDT"],
            timeframe="1h",
            market="crypto",
            data_source="binance_spot",
            account=AccountConfig(currency="USDT", initial_cash=10_000.0),
            mode="backtest",
            cost_overrides={"tick_size": 0.1, "multiplier": 1.0},
            instrument_overrides={"ETHUSDT": {"instrument_type": "spot", "currency": "USDT"}},
        )

        assert CostModel.from_config(cfg, symbol="BTCUSDT").tick_size == 0.1

    def test_unregistered_homogeneous_universe_keeps_the_run_wide_value(self) -> None:
        symbols = ["ZEF202612", "ZEF202703"]
        cfg = self._cfg(
            symbols,
            cost_overrides={"multiplier": 200.0},
            symbol_cost_overrides={symbol: {"commission_per_contract": 0.0} for symbol in symbols},
        )

        assert CostModel.from_config(cfg, symbol="ZEF202703").multiplier == 200.0

    def test_run_wide_value_cannot_replace_a_product_value(self) -> None:
        with pytest.raises(
            ValueError, match=r"multiplier=10\.0 for 'TXF202612', whose product 'TX'"
        ):
            self._cfg(
                ["TXF202612", "MXF202612"],
                cost_overrides={"multiplier": 10.0},
                instrument_overrides={
                    "TXF202612": {"product": "TX", "contract_month": "202612"},
                    "MXF202612": {"product": "MTX", "contract_month": "202612"},
                },
            )


class TestPerContractCommission:
    """commission_per_contract scales with contracts; min_commission floors
    the per-trade total; the fee comes from each symbol's registry entry."""

    @staticmethod
    def _cfg(symbol: str, **kwargs):
        from librae.config.symbols import get_symbol
        from librae.core.run_config import AccountConfig, RunConfig

        info = get_symbol(symbol)
        return RunConfig(
            strategy_name="x",
            symbols=[symbol],
            timeframe="5m",
            market=info.market,
            data_source=info.data_source,
            account=AccountConfig(currency=info.currency, initial_cash=1_000_000.0),
            mode="backtest",
            **kwargs,
        )

    def test_fee_and_rate_add_then_floor_applies_to_the_total(self) -> None:
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.001,
            min_commission=100.0,
            commission_per_contract=40.0,
            slippage_ticks=0.0,
            tick_size=1.0,
            tax_rate=0.0,
        )
        assert cm.calc_commission(1_000.0, 1.0) == pytest.approx(100.0)  # 40 + 1 < floor
        assert cm.calc_commission(1_000.0, 10.0) == pytest.approx(410.0)  # 400 + 10

    @pytest.mark.parametrize(
        ("symbol", "fee"),
        [
            ("TXFR1", 40.0),
            ("TXFR2", 40.0),
            ("MXFR1", 30.0),
            ("MXFR2", 30.0),
            ("TMFR1", 20.0),
            ("TMFR2", 20.0),
        ],
    )
    def test_each_tw_future_charges_its_own_fee_per_contract(self, symbol: str, fee: float) -> None:
        cm = CostModel.from_config(self._cfg(symbol))
        assert cm.commission_per_contract == fee
        assert cm.min_commission == 0.0
        assert cm.calc_commission(22_000.0, 1.0) == pytest.approx(fee)
        assert cm.calc_commission(22_000.0, 10.0) == pytest.approx(10 * fee)

    @pytest.mark.parametrize("side", ["long", "short"])
    def test_buy_and_sell_are_each_charged(self, side: str) -> None:
        from librae.core.executor import calc_trade_pnl

        cm = CostModel.from_config(self._cfg("TXFR1"))
        entry_commission = cm.calc_commission(22_000.0, 10.0)
        pnl = calc_trade_pnl(
            entry_price=22_000.0,
            exit_price=22_100.0,
            quantity=10.0,
            side=side,
            cost_model=cm,
            entry_commission=entry_commission,
            entry_slippage=0.0,
        )
        assert entry_commission == pytest.approx(400.0)
        assert pnl.commission == pytest.approx(800.0)  # 40 x 10 contracts x 2 sides

    def test_run_wide_fee_is_refused_when_products_could_differ(self) -> None:
        from librae.core.run_config import AccountConfig, RunConfig

        with pytest.raises(ValueError, match="symbol_cost_overrides"):
            RunConfig(
                strategy_name="x",
                symbols=["TXFR1", "MXFR1", "TMFR1"],
                timeframe="5m",
                market="tw_futures",
                data_source="shioaji",
                account=AccountConfig(currency="TWD", initial_cash=1_000_000.0),
                mode="backtest",
                cost_overrides={"commission_per_contract": 25.0},
            )

    def test_run_wide_fee_still_sets_a_single_symbol_run(self) -> None:
        cfg = self._cfg("TXFR1", cost_overrides={"commission_per_contract": 25.0})
        assert CostModel.from_config(cfg).commission_per_contract == 25.0

    def test_symbol_cost_overrides_replaces_the_registry_fee(self) -> None:
        cfg = self._cfg("TXFR1", symbol_cost_overrides={"TXFR1": {"commission_per_contract": 25.0}})
        assert CostModel.from_config(cfg).calc_commission(22_000.0, 2.0) == pytest.approx(50.0)

    def test_other_markets_are_unaffected(self) -> None:
        btc = CostModel.from_config(self._cfg("BTCUSDT"))
        btc_quarterly = CostModel.from_config(self._cfg("BTCUSDT_QUARTERLY"))
        mu = CostModel.from_config(self._cfg("MU"))

        assert btc.commission_per_contract == 0.0
        assert btc_quarterly.commission_per_contract == 0.0
        assert mu.commission_per_contract == 0.0
        assert btc.calc_commission(50_000.0, 0.5) == pytest.approx(25.0)  # 0.1% of notional
        assert btc_quarterly.calc_commission(50_000.0, 2.0) == pytest.approx(100.0)
        assert mu.calc_commission(100.0, 10.0) == 0.0

    @staticmethod
    def _unregistered_cfg(symbol: str, *, market: str = "tw_futures", **costs):
        """A contract with no registry entry and no product, routed the way a caller must."""
        from librae.core.run_config import AccountConfig, RunConfig

        return RunConfig(
            strategy_name="x",
            symbols=[symbol],
            timeframe="5m",
            market=market,
            data_source="shioaji",
            account=AccountConfig(currency="TWD", initial_cash=1_000_000.0),
            mode="backtest",
            symbol_cost_overrides={symbol: {"multiplier": 200.0, **costs}},
            instrument_overrides={
                symbol: {
                    "instrument_type": "contract_monthly",
                    "currency": "TWD",
                    "contract_month": "202612",
                    "calendar_id": "XTAIFEX",
                }
            },
        )

    @staticmethod
    def _product_cfg(symbol: str, product: str, **route):
        """An unregistered symbol that names its product and nothing else it shares."""
        from librae.core.run_config import AccountConfig, RunConfig

        return RunConfig(
            strategy_name="x",
            symbols=[symbol],
            timeframe="D1",
            market="tw_futures",
            data_source="my_store",
            account=AccountConfig(currency="TWD", initial_cash=1_000_000.0),
            mode="backtest",
            instrument_overrides={symbol: {"product": product, "data_adapter": "test", **route}},
        )

    @pytest.mark.parametrize(
        ("symbol", "product", "fee", "multiplier"),
        [
            ("TXR1", "TX", 40.0, 200.0),
            ("MTXR2", "MTX", 30.0, 50.0),
            ("TMF_R1", "TMF", 20.0, 10.0),
        ],
    )
    def test_a_symbol_naming_its_product_takes_the_product_fee_and_specs(
        self, symbol: str, product: str, fee: float, multiplier: float
    ) -> None:
        from librae.config.symbols import resolve_symbol

        cfg = self._product_cfg(symbol, product, continuous_alias=True)
        cm = CostModel.from_config(cfg, symbol=symbol)
        instrument = resolve_symbol(cfg, symbol)

        assert cm.commission_per_contract == fee
        assert cm.multiplier == multiplier
        assert cm.calc_commission(22_000.0, 10.0) == pytest.approx(10 * fee)
        assert instrument.product == product
        assert instrument.market == "tw_futures"
        assert instrument.instrument_type == "contract_monthly"
        assert instrument.calendar_id == "XTAIFEX"
        assert instrument.price_increment == 1.0

    @pytest.mark.parametrize(
        ("month", "product", "alias"),
        [("TXF202612", "TX", "TXFR1"), ("MXF202611", "MTX", "MXFR2"), ("TMFH6", "TMF", "TMFR1")],
    )
    def test_contract_month_and_continuous_alias_share_one_fee(
        self, month: str, product: str, alias: str
    ) -> None:
        month_cfg = self._product_cfg(month, product, contract_month="202612")
        month_cm = CostModel.from_config(month_cfg, symbol=month)
        alias_cm = CostModel.from_config(self._cfg(alias))
        assert month_cm.commission_per_contract == alias_cm.commission_per_contract

    def test_registered_symbols_take_their_fee_from_their_product(self) -> None:
        from librae.config.symbols import get_product, load_symbol_registry

        for info in load_symbol_registry().values():
            if info.product is None:
                continue
            product = get_product(info.product)
            cm = CostModel.from_config(self._cfg(info.symbol))
            assert cm.commission_per_contract == product.commission_per_contract
            assert info.multiplier == product.multiplier

    @pytest.mark.parametrize("symbol", ["ZEF202612", "TXF202612", "TXFL6", "TXR1"])
    def test_tw_future_without_a_product_refuses_to_resolve_without_a_fee(
        self, symbol: str
    ) -> None:
        with pytest.raises(ValueError, match=r"\['product'\].*commission_per_contract"):
            CostModel.from_config(self._unregistered_cfg(symbol), symbol=symbol)

    def test_unknown_product_is_refused_with_the_known_ones(self) -> None:
        with pytest.raises(ValueError, match=r"'TXF' is not a known product; one of \['MTX'"):
            self._product_cfg("TXF202612", "TXF", contract_month="202612")

    def test_a_registered_symbol_cannot_change_product(self) -> None:
        with pytest.raises(ValueError, match="contradicts registered symbol 'TXFR1'"):
            self._product_cfg("TXFR1", "MTX")

    def test_unrecognized_tw_future_runs_with_an_explicit_cost_model(self) -> None:
        """An explicit cost model wins over every resolved cost, the fee included."""
        import pandas as pd
        from librae.backtest.engine import Backtest
        from librae.core.strategy import Strategy

        class DoNothing(Strategy):
            def on_bar(self, ctx):
                return []

        ts = pd.date_range("2026-11-02 01:00", periods=3, freq="5min", tz="UTC")
        bars = pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0},
            index=pd.MultiIndex.from_arrays(
                [["ZEF202612"] * len(ts), ts], names=["symbol", "datetime"]
            ),
        )
        cfg = self._unregistered_cfg("ZEF202612")
        Backtest(bars, DoNothing(), config=cfg, cost_model=CostModel.zero())

    @pytest.mark.parametrize("fee", [15.0, 0.0])
    def test_explicit_fee_resolves_an_unrecognized_tw_future(self, fee: float) -> None:
        cfg = self._unregistered_cfg("ZEF202612", commission_per_contract=fee)
        assert CostModel.from_config(cfg, symbol="ZEF202612").commission_per_contract == fee

    def test_explicit_fee_replaces_the_product_fee(self) -> None:
        cfg = self._unregistered_cfg("TXF202612", commission_per_contract=25.0)
        assert CostModel.from_config(cfg, symbol="TXF202612").commission_per_contract == 25.0

    @pytest.mark.parametrize(
        ("symbol", "market"), [("TXF202612", "us_equity"), ("ETHUSDT", "crypto")]
    )
    def test_unregistered_symbols_in_other_markets_still_pay_no_fixed_fee(
        self, symbol: str, market: str
    ) -> None:
        cm = CostModel.from_config(self._unregistered_cfg(symbol, market=market), symbol=symbol)
        market_cm = CostModel.from_market(get_market(market), multiplier=200.0)
        assert cm.commission_per_contract == 0.0
        assert cm.commission_rate == market_cm.commission_rate
        assert cm.min_commission == market_cm.min_commission

    def test_negative_fee_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="commission_per_contract"):
            CostModel(
                multiplier=1.0,
                commission_rate=0.0,
                min_commission=0.0,
                commission_per_contract=-1.0,
                slippage_ticks=0.0,
                tick_size=1.0,
                tax_rate=0.0,
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("multiplier", 0.0),
        ("tick_size", 0.0),
        ("commission_rate", -0.001),
        ("slippage_ticks", -1.0),
        ("long_margin_rate", 0.0),
    ],
)
def test_invalid_cost_inputs_fail_fast(field: str, value: float) -> None:
    values = {
        "multiplier": 1.0,
        "commission_rate": 0.0,
        "min_commission": 0.0,
        "slippage_ticks": 0.0,
        "tick_size": 0.01,
        "tax_rate": 0.0,
        "long_margin_rate": 1.0,
        "short_margin_rate": 1.0,
    }
    values[field] = value
    with pytest.raises(ValueError):
        CostModel(**values)
