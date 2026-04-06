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
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from librae.config.market_config import MarketConfig
    from librae.core.run_config import RunConfig

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
    def from_config(
        cls,
        cfg: RunConfig,
        override: CostModel | None = None,
    ) -> CostModel:
        """Resolve cost model with standard priority: explicit > overrides > from market."""
        if override is not None:
            return override
        from librae.config.market_config import get_market
        mc = get_market(cfg.market)
        if cfg.cost_overrides:
            base = asdict(cls.from_market(mc))
            base.update(cfg.cost_overrides)
            return cls(**base)
        return cls.from_market(mc)

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

    def calc_pnl(self, entry_price: float, exit_price: float, quantity: float) -> float:
        """Gross PnL for a round-trip trade.

        Spot:    (exit - entry) * quantity * 1.0
        Futures: (exit - entry) * quantity * (tick_value / tick_size)
        """
        return (exit_price - entry_price) * quantity * self.multiplier

    def calc_commission(self, price: float, quantity: float) -> float:
        """Single-side commission with minimum floor."""
        notional = price * quantity * self.multiplier
        return max(abs(notional) * self.commission_rate, self.min_commission)

    def calc_slippage(self, quantity: float) -> float:
        """Single-side slippage cost in quote currency."""
        return self.slippage_ticks * self.tick_size * abs(quantity) * self.multiplier

    def calc_tax(self, price: float, quantity: float, *, is_sell: bool) -> float:
        """Transaction tax (applied on sell side only, e.g. TW stock/futures)."""
        if not is_sell or self.transaction_tax <= 0:
            return 0.0
        notional = price * quantity * self.multiplier
        return abs(notional) * self.transaction_tax

    def total_cost(self, price: float, quantity: float, *, is_sell: bool) -> float:
        """Total single-side cost: commission + slippage + tax."""
        return (
            self.calc_commission(price, quantity)
            + self.calc_slippage(quantity)
            + self.calc_tax(price, quantity, is_sell=is_sell)
        )

    def estimate_entry_outlay(self, price: float, quantity: float) -> float:
        """Estimate total cash outlay for entering a position (for sizing)."""
        return price * quantity * self.multiplier + self.total_cost(price, quantity, is_sell=False)
