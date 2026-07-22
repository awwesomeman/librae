"""Thin Yahoo Finance (yfinance) client — no business logic.

Used by ``strategies/module/data/regime.py`` (dxy_trend) — no crypto-exchange or
Shioaji equivalent for a US Dollar Index series, so this is the one place
this repo reaches for yfinance. Requires the ``research`` extra
(``pip install -e '.[research]'``) — yfinance is imported lazily inside
the function so importing this module (or anything that imports it)
doesn't require the extra to be installed.

No API key — yfinance scrapes Yahoo's undocumented internal endpoints, no
official rate limit, but heavy/bursty use gets IP-soft-blocked with 429s
(no published threshold). Not suitable for high-frequency polling.
"""
from __future__ import annotations

import pandas as pd


def fetch_daily(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Raw daily OHLC for `ticker` (e.g. ``"DX-Y.NYB"``) over [start, end].

    Returns columns ``[date, Open, High, Low, Close, Volume]`` — yfinance's
    own column names, un-renamed; flattens the MultiIndex columns yfinance
    returns for a single-ticker download.
    """
    import yfinance as yf

    raw = yf.download(ticker, start=start, end=end, progress=False)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    return raw.reset_index().rename(columns={"Date": "date"})


def fetch_dividends(ticker: str) -> pd.Series:
    """Full ex-dividend history for `ticker` (per-share amount, tz-aware
    DatetimeIndex) — yfinance's own structured field, not a scrape."""
    import yfinance as yf

    return yf.Ticker(ticker).dividends


def fetch_splits(ticker: str) -> pd.Series:
    """Full stock-split history for `ticker` (split ratio, tz-aware
    DatetimeIndex) — same structured field, not a scrape."""
    import yfinance as yf

    return yf.Ticker(ticker).splits


def fetch_valuation_snapshot(ticker: str) -> dict:
    """Current analyst consensus + valuation ratios from Ticker.info — a
    live snapshot, not a historical series (see us_entry_snapshot.py)."""
    import yfinance as yf

    info = yf.Ticker(ticker).info
    return {k: info.get(k) for k in (
        "currentPrice", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
        "numberOfAnalystOpinions", "recommendationKey", "recommendationMean",
        "trailingPE", "forwardPE", "priceToSalesTrailing12Months", "enterpriseToEbitda",
    )}


def fetch_next_earnings_date(ticker: str):
    """Next scheduled earnings date (or None), from Ticker.calendar."""
    import yfinance as yf

    cal = yf.Ticker(ticker).calendar or {}
    dates = cal.get("Earnings Date") or []
    return dates[0] if dates else None


def fetch_option_chain_summary(ticker: str, num_expiries: int = 2) -> pd.DataFrame:
    """Put/call OI for the nearest `num_expiries` expiries — raw
    exchange-reported open interest, a near-term positioning snapshot.

    No IV column: yfinance's ``impliedVolatility`` field is broken for MU
    as of 2026-07 (confirmed — values step in exact powers of 2 regardless
    of expiry, e.g. 0.0156/0.0313/0.0625, not real market IV). Don't
    resurrect it without re-verifying against a source that computes IV
    from actual quotes (e.g. reprice from mid quotes via Black-Scholes).
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    rows = []
    for expiry in t.options[:num_expiries]:
        chain = t.option_chain(expiry)
        calls, puts = chain.calls, chain.puts
        rows.append({
            "expiry": expiry,
            "call_oi": calls["openInterest"].sum(),
            "put_oi": puts["openInterest"].sum(),
            "put_call_oi_ratio": puts["openInterest"].sum() / max(calls["openInterest"].sum(), 1),
        })
    return pd.DataFrame(rows)


def fetch_short_interest_snapshot(ticker: str) -> dict:
    """Raw ``sharesShort``/``dateShortInterest`` (+ prior-month pair) from
    yfinance's ``Ticker.info`` — the same FINRA bi-monthly settlement-date
    short interest data, just pre-parsed by Yahoo. Only ever the current
    reading + one prior month, never a backfillable history — fine for
    ``us_entry_snapshot.py``'s live "what does the market look like right
    now" use case, but that's why the backtestable factor pipeline
    (``us_chip.py``) queries FINRA's own API directly instead of this."""
    import yfinance as yf

    info = yf.Ticker(ticker).info
    return {
        "shares_short": info.get("sharesShort"),
        "date_short_interest": info.get("dateShortInterest"),
        "shares_short_prior_month": info.get("sharesShortPriorMonth"),
        "shares_short_prior_month_date": info.get("sharesShortPreviousMonthDate"),
    }
