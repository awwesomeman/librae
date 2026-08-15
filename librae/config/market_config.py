"""Market-level configuration for backtesting.

A small built-in registry, one entry per market type (crypto, tw_futures,
us_equity), each holding cost parameters shared across every instrument
traded in that market — commission/tax/slippage/margin rate structure, plus
tick_size as a low-stakes approximate default (it only feeds a backtest
slippage-cost estimate, not PnL/margin sizing, so a market-wide value is an
acceptable default for large, homogeneous universes like equities/crypto
spot pairs — override per-symbol in librae/config/symbols.py when precision
matters).

This registry used to live in a bundled markets.yaml; it's a plain Python
dict now — that file was never actually included in the built wheel (only
.py files are, without extra packaging config), so `pip install librae`
raised FileNotFoundError the moment get_market() ran for any built-in
market. A handful of hardcoded entries needs no parser, no packaging
config, and can't go missing from the wheel.

Registering your own market doesn't require editing this file at all:
get_market(name, markets={...}) / CostModel.from_config(config, markets={...})
take a caller-built registry that bypasses this one entirely.

multiplier is NOT here — mirrors mainstream frameworks (e.g. QuantConnect
LEAN's symbol-properties-database.csv, which uses a market-wide wildcard
row for equities but requires an explicit row per specific futures
contract): it directly scales PnL/notional/margin, and for a market with
heterogeneous contracts (e.g. tw_futures: TXF=200 vs MXF=50 vs TMF=10) a
single market-level number is actively wrong for most of them. See
librae/config/symbols.py — spot instruments default to 1.0 automatically
(a mathematical invariant, not a guess); contract_* instruments require it
explicit, no fallback.

Per-instrument details (symbol, min_qty, exchange) belong to the broker layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Who sets margin_rate, not how big it is:
# - unlevered: rate is always 1.0, nothing to set (cash/spot).
# - fixed: rate is set by the exchange/regulator (TAIFEX's published margin,
#   Reg-T/融資 rules) — a position can't choose a different one.
# - dynamic: rate is a trader-chosen leverage (isolated/cross-margin perps).
# See librae/core/cost_model.py's CostModel docstring for how this feeds
# per-instrument margin_mode resolution.
MarginMode = Literal["unlevered", "fixed", "dynamic"]


@dataclass(frozen=True)
class MarketConfig:
    """Market-level configuration for backtesting.

    Used by CostModel.from_market() to build a cost model, along with the
    per-symbol multiplier (always) and tick_size (when overridden) from
    librae/config/symbols.py's SymbolInfo.
    """

    name: str
    commission_rate: float
    min_commission: float
    tax_rate: float
    slippage_ticks: float
    tick_size: float
    long_margin_rate: float
    short_margin_rate: float
    volume_impact_ticks: float = 0.0
    maintenance_margin_rate: float = 0.0
    long_margin_mode: MarginMode = "unlevered"
    short_margin_mode: MarginMode = "unlevered"

    def __post_init__(self) -> None:
        for rate_name, mode_name in (
            ("long_margin_rate", "long_margin_mode"),
            ("short_margin_rate", "short_margin_mode"),
        ):
            if getattr(self, mode_name) == "unlevered" and getattr(self, rate_name) != 1.0:
                raise ValueError(
                    f"{mode_name}='unlevered' requires {rate_name}=1.0, "
                    f"got {getattr(self, rate_name)}"
                )


# Built-in reference markets — see this module's docstring for why these
# are plain dataclass instances rather than a bundled YAML file.
_BUILTIN_MARKETS: dict[str, MarketConfig] = {
    "crypto": MarketConfig(
        name="crypto",
        commission_rate=0.001,
        min_commission=0.0,
        tax_rate=0.0,
        slippage_ticks=2,
        tick_size=0.01,
        long_margin_rate=1.0,
        short_margin_rate=1.0,
    ),
    "tw_futures": MarketConfig(
        name="tw_futures",
        # min_commission is NOT exchange-set — taifex.com.tw's feeSchedules
        # page states the exchange only regulates its own clearing fee; the
        # fee a broker (e.g. Shioaji/永豐期貨) actually charges retail
        # clients is negotiated bilaterally, with no exchange-mandated
        # floor. 100.0 here is a backtest assumption about a typical retail
        # floor, not a verifiable exchange fact — check the specific
        # broker's fee schedule if it matters.
        commission_rate=0.0,
        min_commission=100.0,
        # tax_rate verified via taifex.com.tw: 股價指數期貨類 交易稅 =
        # 契約金額 x 十萬分之2, flat rate regardless of contract size — same
        # for TX/MXF/TMF.
        tax_rate=0.00002,
        slippage_ticks=1,
        tick_size=1.0,
        # long_margin_rate/short_margin_rate verified safe to share across
        # TXF/MXF/TMF (2026-07-20, via taifex.com.tw's 保證金 + 契約規格
        # pages): TAIFEX's published 原始保證金 for TX/MXF/TMF was
        # 636,000/159,000/31,800 TWD (2026-07-06 revision) — an exact
        # 20:5:1 ratio, matching the 200:50:10 multiplier ratio. Margin
        # scales proportionally with contract size, so a single
        # market-level rate (margin / notional) is structurally correct
        # for all three, not an approximation that happens to work for
        # TXF alone. 0.075 reflects that revision at TAIEX ~42,671
        # (636,000 / (42,671 * 200)); TAIFEX revises the absolute NTD
        # figure periodically independent of index level, so this will
        # drift and is not meant to be exact — re-derive from
        # https://www.taifex.com.tw/cht/5/indexMarging when it matters.
        long_margin_rate=0.075,
        short_margin_rate=0.075,
        # Exchange-published, not a leverage the trader picks — see MarginMode.
        long_margin_mode="fixed",
        short_margin_mode="fixed",
    ),
    "us_equity": MarketConfig(
        name="us_equity",
        commission_rate=0.0,
        min_commission=0.0,
        tax_rate=0.0,
        slippage_ticks=2,
        tick_size=0.01,
        long_margin_rate=1.0,
        short_margin_rate=0.5,  # Reg T 50% initial margin for short selling
        short_margin_mode="fixed",  # Reg T sets the 50%, not the trader
    ),
}


def load_market_configs() -> dict[str, MarketConfig]:
    """Return the built-in market registry (a copy — callers can't mutate it)."""
    return dict(_BUILTIN_MARKETS)


def get_market(
    market_name: str,
    markets: dict[str, MarketConfig] | None = None,
) -> MarketConfig:
    """Get a single market config by name.

    markets: a pre-built registry to look up in directly, bypassing the
        built-in one entirely. Lets a caller outside this package register
        its own markets (e.g. `get_market("my_market", markets={...})`)
        without touching librae's source.
    """
    resolved = markets if markets is not None else _BUILTIN_MARKETS
    if market_name not in resolved:
        available = list(resolved.keys())
        raise KeyError(f"Market '{market_name}' not found. Available: {available}")
    return resolved[market_name]
