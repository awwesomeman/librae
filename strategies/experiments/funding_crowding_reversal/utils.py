"""Funding-Rate Crowding Reversal — signal computation.

Tests whether two external-data ingredients add anything on top of a
plain-OHLCV baseline: (1) perpetual funding-rate crowding (persistent
one-sided funding = crowded leveraged positioning, a mean-reversion
candidate) and (2) cross-asset relative momentum (BTC vs ETH) as a
confirmation filter on which side to fade. Ported from a different
project's ``utils/`` (not runnable in this repo) onto
``strategies.module.data.funding``/``cross_asset`` — same hypotheses,
this repo's actual data/engine tooling.

Pure functions: same input -> same output. No position tracking — that's
the Strategy layer's job (see ``factor_research.py``'s candidate class).
"""
from __future__ import annotations

import pandas as pd

from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.utils import merge_htf_column

DEFAULT_PARAMS: dict = {
    "entry_z": 1.5,           # funding_z_3d crowding threshold to fade
    "exit_z": 0.5,            # funding_z_3d threshold to flatten (crowding normalized)
    "trend_lookback": 10,     # daily bars — mom_1D_10, candidate C's gate
    "breakout_lookback": 12,  # hourly bars — mom_1H_12, candidate C's trigger
    "breakout_th": 0.005,
    "exit_th": 0.002,
}


def funding_reversal_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Candidate A: pure contrarian fade of one-sided funding crowding.
    ``funding_z_3d > entry_z`` (crowded long, leveraged longs paying to hold)
    -> fade short; ``< -entry_z`` (crowded short) -> fade long. Exit once
    crowding normalizes back past ``exit_z``."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    z = out["funding_z_3d"]
    out["long_entry"] = z < -p["entry_z"]
    out["short_entry"] = z > p["entry_z"]
    out["long_exit"] = z > -p["exit_z"]
    out["short_exit"] = z < p["exit_z"]
    return out


def funding_relmom_confirm_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Candidate B: same funding-crowding fade, gated by cross-asset relative
    momentum agreeing with the fade direction — e.g. only fade crowded-long
    once the asset is already underperforming the reference asset, instead
    of fading on funding alone."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    z, relmom = out["funding_z_3d"], out["xasset_relmom_24"]
    out["long_entry"] = (z < -p["entry_z"]) & (relmom > 0)
    out["short_entry"] = (z > p["entry_z"]) & (relmom < 0)
    out["long_exit"] = z > -p["exit_z"]
    out["short_exit"] = z < p["exit_z"]
    return out


def compute_daily_trend_bool(gate_1d: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """mom_1D_10 sign on daily closes — candidate C's (no-external-data)
    trend gate, via the shared ``momentum`` operator."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return momentum(gate_1d["close"], p["trend_lookback"]) > 0


def merge_daily_trend_gate(df_1h: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """No-lookahead merge of the daily trend gate onto H1 bars. Run once on
    the full continuous BTC/ETH frame *before* splitting into IS/OOS windows
    so daily bars near a split boundary still see their full lookback.

    ``shift(1)`` is required: ``resample_ohlcv``'s D1 index is left-labeled
    (day D's row starts at day D's 00:00), so an un-shifted backward asof
    merge would leak day D's still-forming close/gate into day D's own H1
    bars — the same look-ahead bug ``trendpullback/utils.py`` and
    ``mtf_trend_rsi/utils.py`` document and fix (see their report.md
    "先修 bug" sections). ``fill_value=True`` keeps the earliest (pre-lookback)
    bars trend-permitting rather than silently blocking every entry.
    """
    gate_1d = resample_ohlcv(df_1h, "1D")
    trend_bool = compute_daily_trend_bool(gate_1d, params).shift(1)
    return merge_htf_column(df_1h, trend_bool, column="daily_trend_up", fill_value=True)


def ohlcv_baseline_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Candidate C: no external data at all — daily trend gate (mom_1D_10,
    already merged via ``merge_daily_trend_gate``) + hourly momentum
    breakout (mom_1H_12). The apples-to-apples comparison point for "does
    external data beat what plain OHLCV already gets you"."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    mom_1h = momentum(out["close"], p["breakout_lookback"])
    trend_up = out["daily_trend_up"]
    out["long_entry"] = trend_up & (mom_1h > p["breakout_th"])
    out["short_entry"] = (~trend_up) & (mom_1h < -p["breakout_th"])
    out["long_exit"] = mom_1h < -p["exit_th"]
    out["short_exit"] = mom_1h > p["exit_th"]
    return out


SIGNAL_FNS = {
    "funding_reversal": funding_reversal_signals,
    "funding_relmom_confirm": funding_relmom_confirm_signals,
    "ohlcv_baseline": ohlcv_baseline_signals,
}


def prepare_signals(df: pd.DataFrame, candidate: str, params: dict | None = None) -> pd.DataFrame:
    """Dispatch to one of the three candidate signal functions above.
    ``ohlcv_baseline`` expects ``daily_trend_up`` already merged via
    ``merge_daily_trend_gate`` (call it once on the full frame beforehand)."""
    return SIGNAL_FNS[candidate](df, params)
