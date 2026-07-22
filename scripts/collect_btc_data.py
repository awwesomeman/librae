#!/usr/bin/env python3
"""Populate the DB with BTC price + positioning/on-chain factors for research.

Same DB-first + gap-fill trigger pattern as collect_mu_data.py — this
script's only job is calling get_ohlcv/get_factor over a date range so the
DB catches up, then reporting what landed. Re-running it is cheap:
already-covered ranges are skipped. Also calls sync_factor_registry() so
data_inventory's frequency column stays populated for these factors.

Quarterly delivery futures (quarterly_futures.py) are deliberately not
included here — those need a literal current-quarter contract symbol
(e.g. "BTCUSDT_260925") and roll-selection logic doesn't exist yet
(deferred, see quarterly_futures.py's docstring); pull those manually.

Usage:
    source .env && python scripts/collect_btc_data.py
    source .env && python scripts/collect_btc_data.py --start 2020-01-01
"""
from __future__ import annotations

import argparse
import logging

from strategies.module.data import defi_tvl, funding, hashrate, mempool_congestion, open_interest, stablecoins  # noqa: F401  (register factor fetchers)
from strategies.module.data.factors import get_factor, sync_factor_registry
from strategies.module.data.ohlcv import get_ohlcv

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

SPOT_SYMBOL = "BTCUSDT"
PERP_SYMBOL_CCXT = "BTC/USDT:USDT"   # funding_rate's ccxt symbol format
PERP_SYMBOL_RAW = "BTCUSDT"          # open_interest/basis_premium's raw format
BTC_SYMBOL = "BTC"                   # hashrate.py/mempool_congestion.py pseudo-symbol
MARKET_WIDE_SYMBOL = "CRYPTO_MARKET"  # stablecoins.py/defi_tvl.py pseudo-symbol

PERP_FACTORS_CCXT = ["funding_rate"]
PERP_FACTORS_RAW = [
    "basis_premium", "open_interest", "open_interest_value",
    "top_trader_ls_account_ratio", "top_trader_ls_position_ratio",
    "global_ls_account_ratio", "taker_buy_sell_ratio",
]
ONCHAIN_FACTORS = ["btc_hashrate", "btc_difficulty", "btc_mempool_tx_count"]
MARKET_WIDE_FACTORS = ["stablecoin_mcap_total", "defi_tvl_total"]


def _print_factor(symbol: str, factor_name: str, start: str, end: str | None) -> None:
    print(f"\n=== {symbol} {factor_name} ===")
    df = get_factor(symbol, factor_name, start=start, end=end)
    if df.empty:
        print("no data")
        return
    print(f"{len(df)} rows, {df['timestamp'].min()} -> {df['timestamp'].max()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2020-01-01", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="End date (YYYY-MM-DD), defaults to now")
    args = parser.parse_args()

    sync_factor_registry()

    print(f"=== {SPOT_SYMBOL} daily OHLCV (data_source=binance_spot) ===")
    ohlcv = get_ohlcv(SPOT_SYMBOL, "1d", data_source="binance_spot", start=args.start, end=args.end)
    print(f"{len(ohlcv)} bars, {ohlcv['timestamp'].min()} -> {ohlcv['timestamp'].max()}" if not ohlcv.empty else "no bars")

    for factor_name in PERP_FACTORS_CCXT:
        _print_factor(PERP_SYMBOL_CCXT, factor_name, args.start, args.end)
    for factor_name in PERP_FACTORS_RAW:
        _print_factor(PERP_SYMBOL_RAW, factor_name, args.start, args.end)
    for factor_name in ONCHAIN_FACTORS:
        _print_factor(BTC_SYMBOL, factor_name, args.start, args.end)
    for factor_name in MARKET_WIDE_FACTORS:
        _print_factor(MARKET_WIDE_SYMBOL, factor_name, args.start, args.end)


if __name__ == "__main__":
    main()
