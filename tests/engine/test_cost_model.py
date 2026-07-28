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
    """multiplier is a required kwarg here — from_market() no longer gets
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
        assert np.isclose(cm.min_commission, 100.0)

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
    """bar_volume/impact_coef default to disabled — every pre-existing
    calc_slippage(qty) call site above is unaffected."""

    def test_bar_volume_omitted_unaffected(self, crypto_cost: CostModel) -> None:
        assert crypto_cost.calc_slippage(0.5) == crypto_cost.calc_slippage(0.5, bar_volume=None)

    def test_zero_impact_coef_unaffected_even_with_bar_volume(self, crypto_cost: CostModel) -> None:
        # crypto_cost fixture has impact_coef=0.0 (dataclass default)
        assert crypto_cost.calc_slippage(0.5) == crypto_cost.calc_slippage(0.5, bar_volume=10.0)

    def test_impact_scales_with_participation(self) -> None:
        cm = CostModel(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=1.0,
            tick_size=0.01,
            tax_rate=0.0,
            impact_coef=10.0,
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
            impact_coef=10.0,
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
        )
        assert cm.liquidation_price(100.0, "long") is None


# ── margin_rate_from_absolute ───────────────────────────────────────────────


class TestMarginRateFromAbsolute:
    def test_taifex_txf_example(self) -> None:
        from librae.core.cost_model import margin_rate_from_absolute

        # TAIFEX 大台, 2026-07-06 revision: NT$636,000 initial margin at
        # index ~42,671, multiplier 200 — matches market_config.py's
        # tw_futures long/short_margin_rate=0.075 (rounded).
        rate = margin_rate_from_absolute(636_000, 42_671, 200.0)
        assert rate == pytest.approx(0.075, abs=0.001)

    def test_zero_or_negative_notional_raises(self) -> None:
        from librae.core.cost_model import margin_rate_from_absolute

        with pytest.raises(ValueError, match="must be positive"):
            margin_rate_from_absolute(1000.0, 0.0, 200.0)


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
# Regression coverage for a real bug: market_config.py's tw_futures multiplier
# (50, matching MXF) was silently applied to TXFR1 (real multiplier 200)
# because from_config() never consulted the symbol registry at all.


class TestFromConfig:
    def _cfg(self, symbol: str, market: str = "tw_futures", data_source: str = "shioaji", **kwargs):
        from librae.core.run_config import RunConfig

        return RunConfig(
            strategy_name="x",
            symbols=[symbol],
            timeframe="5m",
            market=market,
            data_source=data_source,
            initial_balance=100_000.0,
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
            self._cfg("SOME_UNREGISTERED_SYMBOL", cost_overrides={"multiplier": 10.0}),
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
        cm = CostModel.from_config(self._cfg("TXFR1", cost_overrides={"multiplier": 999.0}))
        assert cm.multiplier == 999.0

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
        from librae.core.run_config import RunConfig

        cfg = RunConfig(
            strategy_name="x",
            symbols=["TXFR1", "MU"],
            timeframe="1d",
            market="multi",
            data_source="multi",
            initial_balance=100_000.0,
            mode="backtest",
        )

        futures = CostModel.from_config(cfg, symbol="TXFR1")
        equity = CostModel.from_config(cfg, symbol="MU")

        assert futures.min_commission == 100.0
        assert futures.long_margin_rate == 0.075
        assert equity.min_commission == 0.0
        assert equity.long_margin_rate == 1.0
        assert equity.short_margin_rate == 0.5

    def test_symbol_overrides_unregistered_symbol_without_touching_symbols_yaml(self) -> None:
        cm = CostModel.from_config(
            self._cfg(
                "MY_CUSTOM_SYMBOL",
                market="crypto",
                data_source="x",
                symbol_overrides={"MY_CUSTOM_SYMBOL": {"multiplier": 1.0}},
            )
        )
        assert cm.multiplier == 1.0

    def test_symbol_overrides_wins_over_run_wide_cost_overrides(self) -> None:
        cfg = self._cfg(
            "TXFR1",
            cost_overrides={"multiplier": 111.0},
            symbol_overrides={"TXFR1": {"multiplier": 222.0}},
        )
        assert CostModel.from_config(cfg).multiplier == 222.0

    def test_cost_overrides_still_applies_as_fallback_for_symbols_not_listed(self) -> None:
        """cost_overrides is the run-wide fallback; symbol_overrides only
        overrides the specific symbols it lists."""
        cfg = self._cfg(
            "TXFR1",
            cost_overrides={"multiplier": 111.0},
            symbol_overrides={"MXFR1": {"multiplier": 222.0}},
        )
        assert CostModel.from_config(cfg, symbol="TXFR1").multiplier == 111.0


class TestDescribeSymbols:
    """describe_symbols(): per-symbol resolved config + provenance, for
    confirming what a run will actually apply before trusting a backtest."""

    def _cfg(self, symbols: list[str], market="tw_futures", data_source="shioaji", **kwargs):
        from librae.core.run_config import RunConfig

        return RunConfig(
            strategy_name="x",
            symbols=symbols,
            timeframe="5m",
            market=market,
            data_source=data_source,
            initial_balance=100_000.0,
            mode="backtest",
            **kwargs,
        )

    def test_multiple_registered_futures_report_their_own_multiplier(self) -> None:
        from librae.core.cost_model import describe_symbols

        results = describe_symbols(self._cfg(["TXFR1", "MXFR1"]))

        assert [r.symbol for r in results] == ["TXFR1", "MXFR1"]
        assert results[0].multiplier == 200.0
        assert results[0].multiplier_source == "registry"
        assert results[1].multiplier == 50.0
        assert results[0].error is None

    def test_spot_symbol_reports_auto_default_source(self) -> None:
        from librae.core.cost_model import describe_symbols

        results = describe_symbols(
            self._cfg(["BTCUSDT"], market="crypto", data_source="binance_spot")
        )

        assert results[0].multiplier == 1.0
        assert results[0].multiplier_source == "registry (spot auto-default)"
        assert results[0].tick_size_source == "market_default"

    def test_symbol_override_reported_as_source(self) -> None:
        from librae.core.cost_model import describe_symbols

        cfg = self._cfg(["MXFR1"], symbol_overrides={"MXFR1": {"multiplier": 55.0}})
        results = describe_symbols(cfg)

        assert results[0].multiplier == 55.0
        assert results[0].multiplier_source == "symbol_overrides"

    def test_run_wide_cost_override_reported_as_source(self) -> None:
        from librae.core.cost_model import describe_symbols

        cfg = self._cfg(["UNREGISTERED"], cost_overrides={"multiplier": 10.0})
        results = describe_symbols(cfg)

        assert results[0].multiplier == 10.0
        assert results[0].multiplier_source == "cost_overrides"

    def test_unresolvable_symbol_reports_error_without_raising(self) -> None:
        """Batch of many symbols: one bad entry must not hide the rest."""
        from librae.core.cost_model import describe_symbols

        cfg = self._cfg(["TXFR1", "UNREGISTERED"])
        results = describe_symbols(cfg)

        assert results[0].error is None
        assert results[0].multiplier == 200.0
        assert results[1].error is not None
        assert "multiplier" in results[1].error
        assert results[1].multiplier is None

    def test_defaults_to_cfg_symbols_when_omitted(self) -> None:
        from librae.core.cost_model import describe_symbols

        cfg = self._cfg(["TXFR1", "MXFR1"])
        results = describe_symbols(cfg)

        assert [r.symbol for r in results] == ["TXFR1", "MXFR1"]

    def test_explicit_symbols_arg_overrides_cfg_symbols(self) -> None:
        from librae.core.cost_model import describe_symbols

        cfg = self._cfg(["TXFR1"])
        results = describe_symbols(cfg, symbols=["MXFR1", "TMFR1"])

        assert [r.symbol for r in results] == ["MXFR1", "TMFR1"]


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
