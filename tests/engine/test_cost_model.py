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
        transaction_tax=0.0,
    )


@pytest.fixture
def tw_futures_cost() -> CostModel:
    """TW_TXFR: futures, multiplier=50, min_commission=100, tax on sell."""
    return CostModel(
        multiplier=50.0,
        commission_rate=0.0,
        min_commission=100.0,
        slippage_ticks=1.0,
        tick_size=1.0,
        transaction_tax=0.00002,
    )


# ── CostModel.from_market ─────────────────────────────────────────────


class TestFromMarket:
    def test_crypto_multiplier_is_one(self) -> None:
        market = get_market("crypto")
        cm = CostModel.from_market(market)
        assert np.isclose(cm.multiplier, 1.0)
        assert np.isclose(cm.commission_rate, 0.001)
        assert np.isclose(cm.transaction_tax, 0.0)

    def test_futures_multiplier(self) -> None:
        market = get_market("tw_futures")
        cm = CostModel.from_market(market)
        assert np.isclose(cm.multiplier, 50.0)
        assert np.isclose(cm.min_commission, 100.0)


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
        tax = crypto_cost.calc_tax(50_000.0, 1.0, is_sell=True)
        assert np.isclose(tax, 0.0)

    def test_futures_tax_on_sell(self, tw_futures_cost: CostModel) -> None:
        # 20000 * 1 * 50 * 0.00002 = 20.0
        tax = tw_futures_cost.calc_tax(20_000.0, 1.0, is_sell=True)
        assert np.isclose(tax, 20.0)

    def test_futures_no_tax_on_buy(self, tw_futures_cost: CostModel) -> None:
        tax = tw_futures_cost.calc_tax(20_000.0, 1.0, is_sell=False)
        assert np.isclose(tax, 0.0)


# ── Total cost ────────────────────────────────────────────────────────────


class TestTotalCost:
    def test_crypto_buy_cost(self, crypto_cost: CostModel) -> None:
        # commission=25 + slippage=0.01 + tax=0
        cost = crypto_cost.total_cost(50_000.0, 0.5, is_sell=False)
        assert np.isclose(cost, 25.01)

    def test_futures_sell_cost(self, tw_futures_cost: CostModel) -> None:
        # commission=100 + slippage=50 + tax=20
        cost = tw_futures_cost.total_cost(20_000.0, 1.0, is_sell=True)
        assert np.isclose(cost, 170.0)
