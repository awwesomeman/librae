"""'Is this a short-term entry point right now' snapshot — live market state.

Deliberately outside get_factor()/register_factor_fetcher(): everything
here answers "what does the market look like today", not a point-in-time
series meant to be replayed in a backtest. No coverage-tracking, no cache.
"""
from __future__ import annotations

from strategies.module.data.providers import yahoo


def get_entry_snapshot(symbol: str) -> dict:
    return {
        "valuation": yahoo.fetch_valuation_snapshot(symbol),
        "next_earnings_date": yahoo.fetch_next_earnings_date(symbol),
        "options": yahoo.fetch_option_chain_summary(symbol),
        "short_interest": yahoo.fetch_short_interest_snapshot(symbol),
    }
