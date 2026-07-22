"""台指期/選擇權籌碼與流動性 — FinMind-sourced, TX/MTX/TMF derivatives ecosystem.

Three factor families, all from ``strategies.module.data.providers.finmind`` (see
that module's docstring for the full dataset tracking table):

    twfut_{dealer,trust,foreign}_net_oi              — futures net open interest
    twopt_{dealer,trust,foreign}_{call,put}_net_oi   — options net open interest
    twfut_dealer_mm_volume                            — dealer market-making volume

All three are about the *derivatives* side specifically (positioning in TX
futures/options themselves) — broad cash-market flow (融資融券, 現貨三大法人)
lives in ``tw_market_flow.py`` instead; that's a different underlying
question ("is the cash market crowded") even though it's the same provider.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from strategies.module.data.factors import get_factor, register_factor_fetcher
from strategies.module.data.providers import finmind as finmind_client
from strategies.module.data.utils import taipei_date_to_utc

_SOURCE = "finmind"


def _filtered_net(rows: list[dict], filters: dict, long_col: str, short_col: str) -> pd.DataFrame:
    """Rows matching every (field, value) in `filters`, netted as long - short."""
    records = [
        {"timestamp": taipei_date_to_utc(r["date"]), "value": r[long_col] - r[short_col]}
        for r in rows
        if all(r.get(k) == v for k, v in filters.items())
    ]
    if not records:
        return pd.DataFrame(columns=["timestamp", "value"])
    return pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)


def _sum_volume_by_date(rows: list[dict]) -> pd.DataFrame:
    """Total volume per date, summed across every dealer_code row."""
    totals: dict[str, float] = {}
    for r in rows:
        totals[r["date"]] = totals.get(r["date"], 0.0) + r["volume"]
    if not totals:
        return pd.DataFrame(columns=["timestamp", "value"])
    records = [{"timestamp": taipei_date_to_utc(d), "value": v} for d, v in totals.items()]
    return pd.DataFrame(records).sort_values("timestamp").reset_index(drop=True)


# ---------------------------------------------------------------------------
# TaiwanFuturesInstitutionalInvestors — 三大法人期貨淨未平倉
# ---------------------------------------------------------------------------

def _futures_net_oi_fetcher(institution_key: str):
    label = finmind_client.DERIVATIVES_INSTITUTION_LABELS[institution_key]

    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        futures_id = finmind_client.FUTURES_ID_MAP.get(symbol, symbol)
        rows = finmind_client.fetch("TaiwanFuturesInstitutionalInvestors", start_dt, end_dt, data_id=futures_id)
        return _filtered_net(
            rows, {"institutional_investors": label},
            "long_open_interest_balance_volume", "short_open_interest_balance_volume",
        )

    return _fetch


for _key in finmind_client.INSTITUTION_KEYS:
    register_factor_fetcher(
        f"twfut_{_key}_net_oi", _futures_net_oi_fetcher(_key),
        source=_SOURCE, instrument_type="contract_monthly",
    )


def fetch_twfut_net_oi_history(symbol: str, institution: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached net open-interest history for `symbol` (``'TXFR1'``,
    ``'MXFR1'`` or ``'TMFR1'``) and `institution` (``'dealer'``, ``'trust'``
    or ``'foreign'``) over [start, end]. Positive = that class is net long."""
    if institution not in finmind_client.INSTITUTION_KEYS:
        raise ValueError(f"institution must be one of {sorted(finmind_client.INSTITUTION_KEYS)}, got {institution!r}")
    df = get_factor(symbol, f"twfut_{institution}_net_oi", start=start, end=end)
    return df.rename(columns={"value": "net_oi"})


# ---------------------------------------------------------------------------
# TaiwanOptionInstitutionalInvestors — 三大法人選擇權買權/賣權淨未平倉
# ---------------------------------------------------------------------------

def _option_net_oi_fetcher(institution_key: str, call_put_key: str):
    label = finmind_client.DERIVATIVES_INSTITUTION_LABELS[institution_key]
    call_put_label = "買權" if call_put_key == "call" else "賣權"

    def _fetch(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        option_id = finmind_client.OPTION_ID_MAP.get(symbol, symbol)
        rows = finmind_client.fetch("TaiwanOptionInstitutionalInvestors", start_dt, end_dt, data_id=option_id)
        return _filtered_net(
            rows, {"institutional_investors": label, "call_put": call_put_label},
            "long_open_interest_balance_volume", "short_open_interest_balance_volume",
        )

    return _fetch


for _key in finmind_client.INSTITUTION_KEYS:
    for _cp in ("call", "put"):
        register_factor_fetcher(
            f"twopt_{_key}_{_cp}_net_oi", _option_net_oi_fetcher(_key, _cp),
            source=_SOURCE, instrument_type="contract_monthly",
        )


def fetch_twopt_net_oi_history(symbol: str, institution: str, call_put: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached net open-interest history for options `call_put`
    (``'call'``/``'put'``) leg of `institution` (``'dealer'``/``'trust'``/
    ``'foreign'``) on `symbol`'s options chain (``'TXFR1'`` -> TXO) over
    [start, end]. Positive = that class is net long that leg."""
    if institution not in finmind_client.INSTITUTION_KEYS:
        raise ValueError(f"institution must be one of {sorted(finmind_client.INSTITUTION_KEYS)}, got {institution!r}")
    if call_put not in ("call", "put"):
        raise ValueError(f"call_put must be 'call' or 'put', got {call_put!r}")
    df = get_factor(symbol, f"twopt_{institution}_{call_put}_net_oi", start=start, end=end)
    return df.rename(columns={"value": "net_oi"})


# ---------------------------------------------------------------------------
# TaiwanFuturesDealerTradingVolumeDaily — 期貨自營商造市成交量（流動性代理）
# ---------------------------------------------------------------------------

def _dealer_mm_volume_fetcher(symbol: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
    futures_id = finmind_client.FUTURES_ID_MAP.get(symbol, symbol)
    rows = finmind_client.fetch("TaiwanFuturesDealerTradingVolumeDaily", start_dt, end_dt, data_id=futures_id)
    return _sum_volume_by_date(rows)


register_factor_fetcher("twfut_dealer_mm_volume", _dealer_mm_volume_fetcher, source=_SOURCE, instrument_type="contract_monthly")


def fetch_twfut_dealer_mm_volume_history(symbol: str, start: str, end: str) -> pd.DataFrame:
    """DB-cached daily dealer market-making volume (day + night session
    summed) for `symbol` (``'TXFR1'``, ``'MXFR1'`` or ``'TMFR1'``) — a
    liquidity proxy, not a directional signal."""
    df = get_factor(symbol, "twfut_dealer_mm_volume", start=start, end=end)
    return df.rename(columns={"value": "dealer_mm_volume"})


# ---------------------------------------------------------------------------
# Attach helper
# ---------------------------------------------------------------------------

def attach_tw_futures_chip_features(ohlcv: pd.DataFrame, symbol: str, start: str, end: str) -> pd.DataFrame:
    """Merge every futures/options chip + liquidity factor onto an OHLCV
    DataFrame shaped like ``data.ohlcv.get_ohlcv()``'s output (needs a
    ``timestamp`` column).

    Adds twfut_{dealer,trust,foreign}_net_oi, twopt_{dealer,trust,foreign}_
    {call,put}_net_oi, and twfut_dealer_mm_volume — all forward-filled from
    the last published value, 0.0 where no data exists yet.

    FinMind publishes a trading day's figures the same evening, well before
    the next session opens, so a backward asof-merge against next-day-or-
    later bars carries no look-ahead; intraday bars on the publish day
    itself would — this is a daily/next-day signal, not for intrabar use.
    """
    from strategies.module.data.utils import attach_or_zero_fill

    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    for institution in finmind_client.INSTITUTION_KEYS:
        df = attach_or_zero_fill(
            df, f"twfut_{institution}_net_oi",
            fetch_twfut_net_oi_history(symbol, institution, start, end), "net_oi",
        )
        for call_put in ("call", "put"):
            df = attach_or_zero_fill(
                df, f"twopt_{institution}_{call_put}_net_oi",
                fetch_twopt_net_oi_history(symbol, institution, call_put, start, end), "net_oi",
            )

    df = attach_or_zero_fill(
        df, "twfut_dealer_mm_volume",
        fetch_twfut_dealer_mm_volume_history(symbol, start, end), "dealer_mm_volume",
    )
    return df
