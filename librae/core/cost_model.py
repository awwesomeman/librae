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
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from librae.config.market_config import MarketConfig
    from librae.core.run_config import RunConfig


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
        tax_rate: Per-side tax rate (e.g. 0.00002 for TW futures). Applied symmetrically on both buy and sell.
        long_margin_rate: Fraction of notional deducted from cash when opening a long.
            Spot=1.0 (pay full notional), futures=initial_margin/notional (e.g. 0.067).
        short_margin_rate: Fraction of notional deducted from cash when opening a short.
            US equity=0.5 (Reg T 50%), TW equity=0.9 (融券保證金 90%), futures same as long.
    """

    multiplier: float
    commission_rate: float
    min_commission: float
    slippage_ticks: float
    tick_size: float
    tax_rate: float
    long_margin_rate: float = 1.0
    short_margin_rate: float = 1.0

    @classmethod
    def zero(cls) -> CostModel:
        """Zero-cost model for research or testing."""
        return cls(
            multiplier=1.0, commission_rate=0.0, min_commission=0.0,
            slippage_ticks=0.0, tick_size=0.01, tax_rate=0.0,
            long_margin_rate=1.0, short_margin_rate=1.0,
        )

    @classmethod
    def from_config(
        cls,
        cfg: RunConfig,
        override: CostModel | None = None,
    ) -> CostModel:
        """Resolve cost model with standard priority:
        explicit override > cfg.cost_overrides > symbols.yaml per-symbol
        multiplier > market-level default.

        The per-symbol multiplier step matters whenever a market groups
        instruments with different contract economics under one
        markets.yaml entry (e.g. tw_futures: TXF=200 vs MXF=50 vs TMF=10) —
        see librae/config/symbols.py's SymbolInfo.multiplier.
        """
        if override is not None:
            return override
        from librae.config.market_config import get_market
        mc = get_market(cfg.market)
        base = asdict(cls.from_market(mc))

        from librae.config.symbols import get_symbol
        try:
            sym_multiplier = get_symbol(cfg.symbol).multiplier
        except KeyError:
            sym_multiplier = None
        if sym_multiplier is not None:
            base["multiplier"] = sym_multiplier

        if cfg.cost_overrides:
            base.update(cfg.cost_overrides)
        return cls(**base)

    @classmethod
    def from_market(cls, market: MarketConfig) -> CostModel:
        """Build CostModel from MarketConfig (markets.yaml)."""
        return cls(
            multiplier=market.multiplier,
            commission_rate=market.commission_rate,
            min_commission=market.min_commission,
            slippage_ticks=float(market.slippage_ticks),
            tick_size=market.tick_size if market.tick_size > 0 else 0.01,
            tax_rate=market.tax_rate,
            long_margin_rate=market.long_margin_rate,
            short_margin_rate=market.short_margin_rate,
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

    def calc_tax(self, price: float, quantity: float) -> float:
        """Per-side transaction tax. Applied symmetrically on both buy and sell."""
        if self.tax_rate <= 0:
            return 0.0
        notional = price * quantity * self.multiplier
        return abs(notional) * self.tax_rate

    def total_cost(self, price: float, quantity: float) -> float:
        """Total single-side cost: commission + slippage + tax."""
        return (
            self.calc_commission(price, quantity)
            + self.calc_slippage(quantity)
            + self.calc_tax(price, quantity)
        )

    def margin_rate(self, side: Literal["long", "short"]) -> float:
        """Return margin rate for the given side."""
        return self.short_margin_rate if side == "short" else self.long_margin_rate

    def estimate_entry_outlay(self, price: float, quantity: float, side: Literal["long", "short"]) -> float:
        """Estimate total cash outlay for entering a position (for sizing)."""
        notional = price * quantity * self.multiplier
        return notional * self.margin_rate(side) + self.total_cost(price, quantity)
