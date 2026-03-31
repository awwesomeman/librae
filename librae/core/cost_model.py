"""Unified cost model for backtesting.

Handles commission, slippage, transaction tax, and PnL calculation
for both spot and futures instruments via a single `multiplier` pattern.

Design references:
- Backtrader `cashadjust()`: multiplier unifies spot/futures PnL
- QSTrader `FeeModel`: commission and tax separated
- bt: keep it minimal (< 80 lines)

All parameters are scalars for vectorbt compatibility in future param sweeps.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from librae.config.market_config import MarketConfig

from librae.core import EPSILON

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostModel:
    """Immutable cost/PnL model for a single instrument.

    Attributes:
        multiplier: Contract multiplier. Spot=1.0, futures=tick_value/tick_size.
        commission_rate: Per-side rate (e.g. 0.001 = 10 bps).
        min_commission: Minimum commission per trade (e.g. 100 TWD for TW futures).
        slippage_ticks: Number of ticks slippage per side.
        tick_size: Minimum price increment.
        transaction_tax: Tax rate applied on sell side only (e.g. 0.00002 for TW futures).
    """

    multiplier: float
    commission_rate: float
    min_commission: float
    slippage_ticks: float
    tick_size: float
    transaction_tax: float

    @classmethod
    def zero(cls) -> CostModel:
        """Zero-cost model for research or testing."""
        return cls(
            multiplier=1.0, commission_rate=0.0, min_commission=0.0,
            slippage_ticks=0.0, tick_size=0.01, transaction_tax=0.0,
        )

    @classmethod
    def from_market(cls, market: MarketConfig) -> CostModel:
        """Build CostModel from MarketConfig (markets.yaml)."""
        return cls(
            multiplier=market.multiplier,
            commission_rate=market.commission_rate,
            min_commission=market.min_commission,
            slippage_ticks=float(market.slippage_ticks),
            tick_size=market.tick_size if market.tick_size > 0 else 0.01,
            transaction_tax=market.transaction_tax,
        )

    def calc_pnl(self, entry_price: float, exit_price: float, qty: float) -> float:
        """Gross PnL for a round-trip trade.

        Spot:    (exit - entry) * qty * 1.0
        Futures: (exit - entry) * qty * (tick_value / tick_size)
        """
        return (exit_price - entry_price) * qty * self.multiplier

    def calc_commission(self, price: float, qty: float) -> float:
        """Single-side commission with minimum floor."""
        notional = price * qty * self.multiplier
        return max(abs(notional) * self.commission_rate, self.min_commission)

    def calc_slippage(self, qty: float) -> float:
        """Single-side slippage cost in quote currency."""
        return self.slippage_ticks * self.tick_size * abs(qty) * self.multiplier

    def calc_tax(self, price: float, qty: float, *, is_sell: bool) -> float:
        """Transaction tax (applied on sell side only, e.g. TW stock/futures)."""
        if not is_sell or self.transaction_tax <= 0:
            return 0.0
        notional = price * qty * self.multiplier
        return abs(notional) * self.transaction_tax

    def total_cost(self, price: float, qty: float, *, is_sell: bool) -> float:
        """Total single-side cost: commission + slippage + tax."""
        return (
            self.calc_commission(price, qty)
            + self.calc_slippage(qty)
            + self.calc_tax(price, qty, is_sell=is_sell)
        )

    def estimate_entry_outlay(self, price: float, qty: float) -> float:
        """Estimate total cash outlay for entering a position (for sizing)."""
        return price * qty * self.multiplier + self.total_cost(price, qty, is_sell=False)
