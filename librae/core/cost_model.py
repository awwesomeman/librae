"""Unified cost model for backtesting.

Handles commission, slippage, transaction tax, and PnL calculation
for both spot and futures instruments via a single `multiplier` pattern.
multiplier always comes from the per-symbol registry (librae/config/symbols.py) —
auto-1.0 for spot, required-explicit for contract_* instruments. tick_size
comes from symbols.py when overridden there, otherwise from market_config.py's
market-level approximation (see librae/config/symbols.py's module
docstring for why these two have different risk profiles/fallback rules).

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
        impact_coef: Extra slippage_ticks-equivalent at 100% single-bar volume
            participation, scaled linearly down to 0 at 0% participation.
            0 (default) disables market impact entirely.
        maintenance_margin_rate: Fraction of entry notional required to keep
            a leveraged position open. Single rate for both sides (unlike
            long/short_margin_rate — maintenance-margin tiers on the venues
            this matters for, crypto perps, aren't side-dependent). 0
            (default) disables liquidation simulation entirely.
    """

    multiplier: float
    commission_rate: float
    min_commission: float
    slippage_ticks: float
    tick_size: float
    tax_rate: float
    long_margin_rate: float = 1.0
    short_margin_rate: float = 1.0
    impact_coef: float = 0.0
    maintenance_margin_rate: float = 0.0

    @classmethod
    def zero(cls) -> CostModel:
        """Zero-cost model for research or testing."""
        return cls(
            multiplier=1.0,
            commission_rate=0.0,
            min_commission=0.0,
            slippage_ticks=0.0,
            tick_size=0.01,
            tax_rate=0.0,
            long_margin_rate=1.0,
            short_margin_rate=1.0,
            impact_coef=0.0,
            maintenance_margin_rate=0.0,
        )

    @classmethod
    def from_config(
        cls,
        cfg: RunConfig,
        symbol: str | None = None,
        override: CostModel | None = None,
        markets: dict[str, MarketConfig] | None = None,
    ) -> CostModel:
        """Resolve cost model for one symbol, with standard priority:
        explicit override > cfg.symbol_overrides[symbol] > cfg.cost_overrides
        (run-wide fallback) > the built-in symbol registry's per-symbol
        multiplier (spot auto-1.0, contract_* required-explicit) and
        tick_size (optional override) > market-level defaults for
        everything else (see librae/config/symbols.py's module docstring).

        symbol: which symbol to resolve for — defaults to cfg.symbol
            (symbols[0]). Pass explicitly to resolve a different symbol in
            a multi-asset run, e.g. one with a different contract
            multiplier (TXFR1=200 vs MXFR1=50, same tw_futures market).

        markets: pre-built registry passed through to get_market() —
            lets a caller resolve cfg.market against markets it built
            itself instead of librae's built-in market registry.

        Raises:
            ValueError: `symbol` isn't in the built-in registry (so no
                multiplier can be resolved at all — not even the spot
                auto-default, since we don't know the instrument_type) and
                neither cfg.symbol_overrides[symbol] nor cfg.cost_overrides
                supplies multiplier either.
        """
        if override is not None:
            return override
        symbol = symbol if symbol is not None else cfg.symbol

        from librae.config.symbols import get_symbol

        try:
            sym = get_symbol(symbol)
            multiplier, tick_size = sym.multiplier, sym.tick_size
            symbol_market = sym.market
        except KeyError:
            multiplier, tick_size = None, None
            symbol_market = cfg.market

        route = (cfg.instrument_overrides or {}).get(symbol, {})
        symbol_market = route.get("market", symbol_market)

        from librae.config.market_config import get_market

        mc = get_market(symbol_market, markets=markets)

        # symbol_overrides[symbol] wins over the run-wide cost_overrides
        # fallback for anything both specify.
        overrides = dict(cfg.cost_overrides or {})
        overrides.update((cfg.symbol_overrides or {}).get(symbol, {}))

        multiplier = overrides.get("multiplier", multiplier)
        tick_size = overrides.get("tick_size", tick_size)
        if multiplier is None:
            raise ValueError(
                f"No multiplier for symbol={symbol!r} — pass cfg.symbol_overrides={{"
                f"{symbol!r}: {{'multiplier': ...}}}} (or cfg.cost_overrides for a "
                "single-symbol run), or register it in librae/config/symbols.py's "
                "built-in registry (spot instruments default to 1.0 automatically; "
                "contract_* instruments need it explicit)."
            )

        base = asdict(cls.from_market(mc, multiplier=multiplier, tick_size=tick_size))
        base.update(overrides)
        return cls(**base)

    @classmethod
    def from_market(
        cls,
        market: MarketConfig,
        *,
        multiplier: float,
        tick_size: float | None = None,
    ) -> CostModel:
        """Build CostModel from MarketConfig (librae/config/market_config.py) + a per-symbol
        multiplier (required — see librae/config/symbols.py). tick_size
        defaults to the market's approximate value when not overridden."""
        return cls(
            multiplier=multiplier,
            commission_rate=market.commission_rate,
            min_commission=market.min_commission,
            slippage_ticks=float(market.slippage_ticks),
            tick_size=tick_size if tick_size is not None else market.tick_size,
            tax_rate=market.tax_rate,
            long_margin_rate=market.long_margin_rate,
            short_margin_rate=market.short_margin_rate,
            impact_coef=market.impact_coef,
            maintenance_margin_rate=market.maintenance_margin_rate,
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

    def calc_slippage(self, quantity: float, *, bar_volume: float | None = None) -> float:
        """Single-side slippage cost in quote currency.

        bar_volume (optional): when given together with impact_coef > 0,
        adds a market-impact component that scales linearly with how much
        of the bar's volume this fill consumes. Omitted (the default at
        every existing call site) reproduces the flat slippage_ticks-only
        cost unchanged.
        """
        ticks = self.slippage_ticks
        if bar_volume and bar_volume > 0 and self.impact_coef > 0:
            ticks += self.impact_coef * (abs(quantity) / bar_volume)
        return ticks * self.tick_size * abs(quantity) * self.multiplier

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

    def estimate_entry_outlay(
        self, price: float, quantity: float, side: Literal["long", "short"]
    ) -> float:
        """Estimate total cash outlay for entering a position (for sizing)."""
        notional = price * quantity * self.multiplier
        return notional * self.margin_rate(side) + self.total_cost(price, quantity)

    def liquidation_price(self, entry_price: float, side: Literal["long", "short"]) -> float | None:
        """Approximate isolated-margin liquidation price (ignores fees/
        funding, same simplification level as the rest of this margin
        model). None if maintenance_margin_rate is disabled (0, the
        default) or the side's margin_rate doesn't leave a valid
        maintenance buffer (misconfigured — fail safe, not a crash, since
        this is a safety feature).

        Derivation (long): liquidates when margin_locked + unrealized_pnl
        <= maintenance_requirement, i.e.
        entry_notional*margin_rate + (mark-entry)*qty*mult <= entry_notional*maintenance_margin_rate.
        Solving for mark (entry_notional = entry_price*qty*mult cancels
        qty*mult) gives the formula below; short is the mirror image.
        """
        if self.maintenance_margin_rate <= 0:
            return None
        margin_rate = self.margin_rate(side)
        if margin_rate <= self.maintenance_margin_rate:
            return None
        if side == "long":
            return entry_price * (1 + self.maintenance_margin_rate - margin_rate)
        return entry_price * (1 - self.maintenance_margin_rate + margin_rate)


@dataclass(frozen=True)
class SymbolDescription:
    """One symbol's resolved cost-model config, plus where each value came
    from — see describe_symbols()."""

    symbol: str
    market: str
    data_source: str
    multiplier: float | None
    multiplier_source: str
    tick_size: float | None
    tick_size_source: str
    long_margin_rate: float | None
    short_margin_rate: float | None
    error: str | None = None


def describe_symbols(cfg: RunConfig, symbols: list[str] | None = None) -> list[SymbolDescription]:
    """Resolve and report each symbol's cost-model config (multiplier,
    tick_size, margin rates) plus where each value came from —
    'symbol_overrides' > 'cost_overrides' > 'registry' > 'market_default',
    matching CostModel.from_config's actual priority. A diagnostic to
    confirm what's really applied before trusting a backtest, not something
    the engine consults itself.

    symbols defaults to cfg.symbols when omitted. A symbol that can't
    resolve (no multiplier anywhere) is reported with its error message
    instead of raising, so one bad entry in a large batch doesn't hide the
    rest — this is the main reason to call this on a whole symbol list at
    once rather than one at a time.
    """
    from librae.config.symbols import get_symbol

    results: list[SymbolDescription] = []
    for sym in symbols if symbols is not None else cfg.symbols:
        symbol_over = (cfg.symbol_overrides or {}).get(sym, {})
        cost_over = cfg.cost_overrides or {}
        try:
            info = get_symbol(sym)
        except KeyError:
            info = None
        route = (cfg.instrument_overrides or {}).get(sym, {})
        resolved_market = route.get("market") or (info.market if info else cfg.market)
        resolved_source = route.get("data_source") or (
            info.data_source if info else cfg.data_source
        )

        try:
            cm = CostModel.from_config(cfg, symbol=sym)
        except (KeyError, ValueError) as e:
            results.append(
                SymbolDescription(
                    symbol=sym,
                    market=resolved_market,
                    data_source=resolved_source,
                    multiplier=None,
                    multiplier_source="unresolved",
                    tick_size=None,
                    tick_size_source="unresolved",
                    long_margin_rate=None,
                    short_margin_rate=None,
                    error=str(e),
                )
            )
            continue

        if "multiplier" in symbol_over:
            multiplier_source = "symbol_overrides"
        elif "multiplier" in cost_over:
            multiplier_source = "cost_overrides"
        elif info is not None and info.instrument_type == "spot":
            multiplier_source = "registry (spot auto-default)"
        else:
            multiplier_source = "registry"

        if "tick_size" in symbol_over:
            tick_size_source = "symbol_overrides"
        elif "tick_size" in cost_over:
            tick_size_source = "cost_overrides"
        elif info is not None and info.tick_size is not None:
            tick_size_source = "registry"
        else:
            tick_size_source = "market_default"

        results.append(
            SymbolDescription(
                symbol=sym,
                market=resolved_market,
                data_source=resolved_source,
                multiplier=cm.multiplier,
                multiplier_source=multiplier_source,
                tick_size=cm.tick_size,
                tick_size_source=tick_size_source,
                long_margin_rate=cm.long_margin_rate,
                short_margin_rate=cm.short_margin_rate,
            )
        )
    return results


def margin_rate_from_absolute(
    absolute_margin: float, reference_price: float, multiplier: float
) -> float:
    """Convert an exchange-published absolute margin figure (e.g. TAIFEX's
    NT$636,000 initial margin per 大台 contract) into the margin_rate ratio
    CostModel/MarketConfig actually take — see market_config.py's tw_futures
    entry for why the ratio, not the absolute figure, is what this engine
    treats as stable (docs/plans/enhance_librae_real_trade.md item B).

    Args:
        absolute_margin: The published currency amount (per contract for
            futures; per share/unit for anything multiplier-scaled).
        reference_price: The underlying's price/index level the absolute
            figure was published against.
        multiplier: The contract's multiplier (librae/config/symbols.py).

    Example: TAIFEX 大台, 2026-07-06 revision —
        margin_rate_from_absolute(636_000, 42_671, 200.0) ≈ 0.0745, matching
        market_config.py's tw_futures long/short_margin_rate=0.075.
    """
    notional = reference_price * multiplier
    if notional <= 0:
        raise ValueError(
            f"reference_price*multiplier must be positive, got {notional} "
            f"(reference_price={reference_price}, multiplier={multiplier})"
        )
    return absolute_margin / notional
