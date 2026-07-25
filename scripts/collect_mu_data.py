#!/usr/bin/env python3
"""Populate the DB with MU (Micron) price + fundamentals for research.

Just calls get_ohlcv/get_factor over a date range — both are DB-first with
API gap-fill, so this script's only job is to trigger that fill and report
what landed. Re-running it is cheap: already-covered ranges are skipped.

Usage:
    source .env && python scripts/collect_mu_data.py
    source .env && python scripts/collect_mu_data.py --start 2020-01-01
"""

from __future__ import annotations

import argparse
import logging

from strategies.module.data import (
    macro,  # noqa: F401  (registers factor fetchers)
    us_chip,  # noqa: F401  (registers factor fetchers)
    us_corporate_actions,  # noqa: F401  (registers factor fetchers)
    us_fundamentals,  # noqa: F401  (registers factor fetchers)
    us_insider,  # noqa: F401  (registers factor fetchers)
)
from strategies.module.data.factors import get_factor, sync_factor_registry
from strategies.module.data.ohlcv import get_ohlcv
from strategies.module.data.us_analyst import (
    collect_analyst_recommendation,
    collect_earnings_surprise,
    load_analyst_recommendation,
    load_earnings_surprise,
)
from strategies.module.data.us_social import (
    collect_social_mentions,
    load_social_mentions,
    load_social_rank,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SYMBOL = "MU"
SECTOR_CONTEXT_SYMBOL = (
    "SOXX"  # semiconductor sector ETF — relative-strength context, zero new code
)
FUNDAMENTAL_FACTORS = [
    "us_revenue",
    "us_net_income",
    "us_eps_diluted",
    "us_gross_profit",
    "us_operating_income",
    "us_operating_cash_flow",
    "us_capex",
    "us_inventory",
    "us_shares_outstanding",
]
CORPORATE_ACTION_FACTORS = ["us_dividend", "us_split"]
INSIDER_FACTORS = ["us_insider_net_shares"]
CHIP_FACTORS = ["us_short_interest", "us_short_volume_ratio"]
MACRO_FACTORS = [
    "us_credit_spread",
    "us_financial_conditions",
    "us_mortgage_rate",
    "us_vix",
    "us_yield_curve_10y2y",
    "us_semiconductor_production",
    "us_supply_chain_pressure",
]
MACRO_SYMBOL = "MACRO"  # pseudo-symbol — see macro.py's module docstring


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2015-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD), defaults to now")
    args = parser.parse_args()

    sync_factor_registry()

    print(f"=== {SYMBOL} daily OHLCV (data_source=yahoo) ===")
    ohlcv = get_ohlcv(SYMBOL, "1d", data_source="yahoo", start=args.start, end=args.end)
    print(
        f"{len(ohlcv)} bars, {ohlcv['timestamp'].min()} -> {ohlcv['timestamp'].max()}"
        if not ohlcv.empty
        else "no bars"
    )

    for factor_name in (
        FUNDAMENTAL_FACTORS + CORPORATE_ACTION_FACTORS + INSIDER_FACTORS + CHIP_FACTORS
    ):
        print(f"\n=== {SYMBOL} {factor_name} ===")
        df = get_factor(SYMBOL, factor_name, start=args.start, end=args.end)
        if df.empty:
            print("no data")
            continue
        print(df.to_string(index=False))

    print(f"\n=== {SYMBOL} us_social_mentions/us_social_rank (source=apewisdom, append-only) ===")
    n = collect_social_mentions(SYMBOL)
    print(f"{n} new row(s) written")
    mentions = load_social_mentions(SYMBOL, start=args.start, end=args.end)
    rank = load_social_rank(SYMBOL, start=args.start, end=args.end)
    print(mentions.to_string(index=False) if not mentions.empty else "no mentions data")
    print(rank.to_string(index=False) if not rank.empty else "no rank data")

    for factor_name in MACRO_FACTORS:
        print(f"\n=== {MACRO_SYMBOL} {factor_name} (source=fred) ===")
        df = get_factor(MACRO_SYMBOL, factor_name, start=args.start, end=args.end)
        print(df.to_string(index=False) if not df.empty else "no data")

    print(f"\n=== {SYMBOL} us_analyst_recommendation_score (source=finnhub, append-only) ===")
    n = collect_analyst_recommendation(SYMBOL)
    print(f"{n} new row(s) written")
    reco = load_analyst_recommendation(SYMBOL, start=args.start, end=args.end)
    print(reco.to_string(index=False) if not reco.empty else "no data")

    print(f"\n=== {SYMBOL} us_earnings_surprise_pct (source=finnhub, append-only) ===")
    n = collect_earnings_surprise(SYMBOL)
    print(f"{n} new row(s) written")
    surprise = load_earnings_surprise(SYMBOL, start=args.start, end=args.end)
    print(surprise.to_string(index=False) if not surprise.empty else "no data")

    print(f"\n=== {SECTOR_CONTEXT_SYMBOL} daily OHLCV (sector context, data_source=yahoo) ===")
    sector = get_ohlcv(
        SECTOR_CONTEXT_SYMBOL, "1d", data_source="yahoo", start=args.start, end=args.end
    )
    if not sector.empty:
        print(f"{len(sector)} bars, {sector['timestamp'].min()} -> {sector['timestamp'].max()}")


if __name__ == "__main__":
    main()
