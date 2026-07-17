"""Cross-asset linkage factors — rolling correlation and relative-strength
momentum between a primary asset and a reference asset (e.g. BTC vs ETH).

Only meaningful for two assets on a shared, overlapping trading calendar —
24/7-vs-24/7 crypto pairs are clean; pairing a 24/7 asset against a
day-session-only market (e.g. TXFR1) would silently misalign most rows via
the asof-merge below unless the join is made session-aware first (see
brokers/taipei_time.py for the session-boundary logic that would be needed).
"""
from __future__ import annotations

import pandas as pd

from data.ohlcv import get_ohlcv


def attach_cross_asset_features(
    ohlcv: pd.DataFrame,
    ref_symbol: str,
    ref_timeframe: str,
    ref_data_source: str,
    start: str,
    end: str,
    window: int = 24,
) -> pd.DataFrame:
    """Attach rolling correlation and relative-momentum factors between
    `ohlcv` (the primary asset's frame, shaped like ``data.ohlcv.get_ohlcv()``'s
    output) and a reference asset's closes, asof-merged backward on
    `timestamp` — each primary-asset bar only sees reference-asset bars at or
    before its own timestamp, so no cross-asset look-ahead.

    Adds ``xasset_corr_{window}`` (rolling Pearson correlation of period
    returns) and ``xasset_relmom_{window}`` (own {window}-period momentum
    minus reference-asset {window}-period momentum — positive means
    outperforming the reference asset over that window).
    """
    df = ohlcv.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    ref = get_ohlcv(ref_symbol, ref_timeframe, data_source=ref_data_source, start=start, end=end)
    ref = ref[["timestamp", "close"]].rename(columns={"close": "ref_close"}).sort_values("timestamp")
    ref["timestamp"] = pd.to_datetime(ref["timestamp"], utc=True)
    ref["ref_ret"] = ref["ref_close"].pct_change()

    df = df.sort_values("timestamp")
    df["timestamp"] = df["timestamp"].astype("datetime64[ns, UTC]")
    ref["timestamp"] = ref["timestamp"].astype("datetime64[ns, UTC]")
    df = pd.merge_asof(df, ref[["timestamp", "ref_close", "ref_ret"]], on="timestamp", direction="backward")

    own_ret = df["close"].pct_change()
    df[f"xasset_corr_{window}"] = own_ret.rolling(window).corr(df["ref_ret"])

    own_mom = df["close"] / df["close"].shift(window) - 1.0
    ref_mom = df["ref_close"] / df["ref_close"].shift(window) - 1.0
    df[f"xasset_relmom_{window}"] = own_mom - ref_mom

    return df.drop(columns=["ref_close", "ref_ret"])
