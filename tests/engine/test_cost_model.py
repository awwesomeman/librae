"""Tests for CostModel: spot vs futures PnL, commission, slippage, tax."""
from __future__ import annotations

import numpy as np
import pytest

from librae.core.cost_model import CostModel
from librae.config.market_config import get_market


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
# Regression coverage for a real bug: markets.yaml's tw_futures multiplier
# (50, matching MXF) was silently applied to TXFR1 (real multiplier 200)
# because from_config() never consulted symbols.yaml at all.


class TestFromConfig:
    def _cfg(self, symbol: str, market: str = "tw_futures", data_source: str = "shioaji", **kwargs):
        from librae.core.run_config import RunConfig
        return RunConfig(
            strategy_name="x", symbols=[symbol], timeframe="5m", market=market,
            data_source=data_source, initial_balance=100_000.0, mode="backtest", **kwargs,
        )

    def test_registered_symbol_uses_its_own_multiplier(self) -> None:
        cm = CostModel.from_config(self._cfg("TXFR1"))
        assert cm.multiplier == 200.0  # not markets.yaml's tw_futures default (50)

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
        cm = CostModel.from_config(self._cfg("BTCUSDT", market="crypto", data_source="binance_spot"))
        assert cm.multiplier == 1.0

    def test_spot_symbol_without_explicit_multiplier_defaults_to_one(self) -> None:
        """BTCUSDT doesn't declare multiplier in symbols.yaml at all — spot
        auto-defaults, no per-symbol registration needed."""
        from librae.config.symbols import get_symbol
        assert get_symbol("BTCUSDT").multiplier == 1.0

    def test_spot_symbol_tick_size_falls_back_to_market_default(self) -> None:
        cm = CostModel.from_config(self._cfg("BTCUSDT", market="crypto", data_source="binance_spot"))
        assert cm.tick_size == 0.01  # crypto market default — BTCUSDT doesn't override it

    def test_explicit_cost_overrides_win_over_symbol_multiplier(self) -> None:
        cm = CostModel.from_config(self._cfg("TXFR1", cost_overrides={"multiplier": 999.0}))
        assert cm.multiplier == 999.0

    def test_explicit_override_object_wins_over_everything(self) -> None:
        explicit = CostModel.zero()
        cm = CostModel.from_config(self._cfg("TXFR1"), override=explicit)
        assert cm is explicit
