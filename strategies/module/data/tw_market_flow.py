"""全市場資金流 — FinMind-sourced, TWSE cash market (not derivatives-specific).

Two factor families from ``strategies.module.data.providers.finmind`` (see that
module's docstring for the full dataset tracking table):

    tw_market_margin_balance, tw_market_short_balance          — market-wide 融資融券餘額
    tw_market_{dealer,trust,foreign,total}_net_buy             — market-wide 三大法人現貨買賣超

Neither is tied to a specific instrument — both are national aggregates,
answering "is the cash market crowded/one-sided" rather than "who's
positioned how in TX futures/options" (that's ``tw_futures_chip.py``).
Cached under a fixed pseudo-symbol (``_MARKET_WIDE_SYMBOL``) since
``get_factor``'s cache key always needs a symbol, even though none of
these have a real underlying instrument.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import finmind as finmind_client
from strategies.module.data.utils import taipei_date_to_utc

_SOURCE = "finmind"
_MARKET_WIDE_SYMBOL = "TWSE"


def _by_name(rows: list[dict], name: str, value_field: str) -> pd.DataFrame:
    """Rows where row['name'] == name, value taken directly from `value_field`."""
    records = [{"timestamp": taipei_date_to_utc(r["date"]), "value": r[value_field]} for r in rows if r["name"] == name]
    if not records:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)


def _net_buy_by_name(rows: list[dict], name: str) -> pd.DataFrame:
    """Rows where row['name'] == name, value = buy - sell."""
    records = [{"timestamp": taipei_date_to_utc(r["date"]), "value": r["buy"] - r["sell"]} for r in rows if r["name"] == name]
    if not records:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)


# ---------------------------------------------------------------------------
# TaiwanStockTotalMarginPurchaseShortSale — 全市場融資融券餘額
# ---------------------------------------------------------------------------

def _margin_balance_fetcher(name: str):
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        rows = finmind_client.fetch("TaiwanStockTotalMarginPurchaseShortSale", start_dt, end_dt)
        return _by_name(rows, name, "TodayBalance")

    return _fetch


register_factor_fetcher("tw_market_margin_balance", _margin_balance_fetcher("MarginPurchase"), source=_SOURCE, instrument_type="spot")
register_factor_fetcher("tw_market_short_balance", _margin_balance_fetcher("ShortSale"), source=_SOURCE, instrument_type="spot")


def fetch_tw_margin_balance_history(factor: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached market-wide balance history — `factor` is ``'tw_market_margin_balance'``
    (融資餘額) or ``'tw_market_short_balance'``(融券餘額)."""
    if factor not in ("tw_market_margin_balance", "tw_market_short_balance"):
        raise ValueError(f"factor must be 'tw_market_margin_balance' or 'tw_market_short_balance', got {factor!r}")
    df = get_factor(_MARKET_WIDE_SYMBOL, factor, start=start, end=end)
    return df.rename(columns={"value": "balance"})


# ---------------------------------------------------------------------------
# TaiwanStockTotalInstitutionalInvestors — 全市場三大法人現貨買賣超
# ---------------------------------------------------------------------------

def _market_net_buy_fetcher(name: str):
    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        rows = finmind_client.fetch("TaiwanStockTotalInstitutionalInvestors", start_dt, end_dt)
        return _net_buy_by_name(rows, name)

    return _fetch


for _key, _label in finmind_client.MARKET_INSTITUTION_LABELS.items():
    register_factor_fetcher(
        f"tw_market_{_key}_net_buy", _market_net_buy_fetcher(_label),
        source=_SOURCE, instrument_type="spot",
    )


def fetch_tw_market_net_buy_history(institution: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached market-wide net-buy history (TWD) for `institution`
    (``'dealer'``/``'trust'``/``'foreign'``/``'total'``)."""
    if institution not in finmind_client.MARKET_INSTITUTION_LABELS:
        raise ValueError(f"institution must be one of {sorted(finmind_client.MARKET_INSTITUTION_LABELS)}, got {institution!r}")
    df = get_factor(_MARKET_WIDE_SYMBOL, f"tw_market_{institution}_net_buy", start=start, end=end)
    return df.rename(columns={"value": "net_buy"})


# ---------------------------------------------------------------------------
# Attach helper
# ---------------------------------------------------------------------------

def attach_tw_market_flow_features(ohlcv: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Merge every market-wide flow factor onto an OHLCV DataFrame shaped
    like ``data.ohlcv.get_ohlcv()``'s output (needs a ``timestamp`` column).

    No `symbol` argument — every factor here is a national aggregate, not
    tied to whatever instrument `ohlcv` happens to be. Adds
    tw_market_margin_balance, tw_market_short_balance, and
    tw_market_{dealer,trust,foreign,total}_net_buy —
    all forward-filled from the last published value, 0.0 where no data
    exists yet.

    Same backward-asof/no-look-ahead reasoning as
    ``tw_futures_chip.attach_tw_futures_chip_features`` — see that
    function's docstring.
    """
    from strategies.module.data.utils import attach_or_zero_fill

    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    df = attach_or_zero_fill(df, "tw_market_margin_balance", fetch_tw_margin_balance_history("tw_market_margin_balance", start, end), "balance")
    df = attach_or_zero_fill(df, "tw_market_short_balance", fetch_tw_margin_balance_history("tw_market_short_balance", start, end), "balance")
    for institution in ("dealer", "trust", "foreign", "total"):
        df = attach_or_zero_fill(
            df, f"tw_market_{institution}_net_buy",
            fetch_tw_market_net_buy_history(institution, start, end), "net_buy",
        )

    return df
