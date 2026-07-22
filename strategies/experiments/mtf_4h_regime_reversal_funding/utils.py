"""MTF 4H Regime-Switching Reversal + Funding — daily |mom_1D_10| regime
gate switches between two 4H sub-strategies: range-mode reversal
(roc_3/rsi_6 extremes) when the daily trend is weak, trend-mode funding
momentum (crowded-long/short funding confirms the daily trend) when it's
strong.

Ported from ``strategies/experiments/mtf_4h_regime_reversal_funding/`` (a
different project's ``utils/``, not runnable in this repo). The daily gate
merge follows the same no-lookahead pattern
``strategies/experiments/trendpullback/utils.py`` and
``strategies/experiments/mtf_trend_rsi/utils.py`` use: a D1 bar's own value
isn't fully known until D1 closes, so the gate must be shifted by one bar
before merging onto 4H.

Pure functions: same input -> same output, no I/O (funding-rate fetch stays
in ``factor_research.py``, which attaches it before calling
``prepare_signals``). No position tracking — that's the Strategy layer's job.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.utils import merge_htf_column

DEFAULT_PARAMS: dict = {
    "mom_lookback": 10,        # daily bars — mom_1D_10's own definition
    "regime_thresh": 0.03,     # |mom_1D_10| >= this => trending, else ranging
    "roc_period": 3,
    "rsi_period": 6,
    "roc_buy": -0.01, "rsi_buy": -35.0,     # range-mode long entry
    "roc_sell": 0.025, "rsi_sell": 35.0,    # range-mode short entry
    "rsi_exit_band": 3.0,                    # range-mode exit: |rsi_6| <= band
    "funding_z_long": 1.5, "funding_z_short": -2.0,   # trend-mode entry
    "funding_exit_long": 0.8, "funding_exit_short": -0.8,  # trend-mode exit
    "max_hold_ranging": 12,    # 4H bars (48h)
    "max_hold_trending": 16,   # 4H bars (64h)
    "vwap_window": 12,         # daily bars — vwap_dist_12's own definition
    # Ablation override for stage-5 candidate comparison: None runs the
    # deployed regime-switching logic; "ranging"/"trending" forces the gate
    # so the range-mode-only / trend-mode-only sub-strategies can be
    # compared against the switching composite on equal footing.
    "force_regime": None,
}


def compute_daily_features(d1: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Daily-bar features feeding the regime gate + the standalone
    vwap_dist_12 factor test. Returns a *copy* of `d1` with added columns:
    mom_1D_10, is_ranging, mom_up, vwap_dist_12. All computed from D1's own
    OHLCV only — not yet safe to merge onto 4H without shifting (see
    ``merge_regime_gate``)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = d1.copy()
    out["mom_1D_10"] = momentum(out["close"], p["mom_lookback"])
    out["is_ranging"] = out["mom_1D_10"].abs() < p["regime_thresh"]
    out["mom_up"] = out["mom_1D_10"] > 0

    typical_price = (out["high"] + out["low"] + out["close"]) / 3.0
    n = p["vwap_window"]
    vwap = (typical_price * out["volume"]).rolling(n).sum() / out["volume"].rolling(n).sum()
    out["vwap_dist_12"] = out["close"] / vwap - 1.0
    return out


def merge_regime_gate(detail: pd.DataFrame, d1_features: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """No-lookahead merge of the daily regime gate onto 4H. ``shift(1)``
    makes a D1 bar's index carry the *previous*, already-completed D1 bar's
    is_ranging/mom_up — mirrors ``mtf_trend_rsi/utils.py::merge_daily_gate``'s
    fix for the identical same-day leak. ``force_regime`` (ablation) is
    applied after the merge, overriding is_ranging for every bar."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    is_ranging = d1_features["is_ranging"].shift(1)
    mom_up = d1_features["mom_up"].shift(1)

    out = merge_htf_column(detail, is_ranging, column="is_ranging", fill_value=True)
    out = merge_htf_column(out, mom_up, column="mom_up", fill_value=True)

    if p["force_regime"] == "ranging":
        out["is_ranging"] = True
    elif p["force_regime"] == "trending":
        out["is_ranging"] = False
    return out


def compute_4h_features(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """roc_3 + demeaned rsi_6 on the 4H base timeframe. Both use only the
    current and past 4H closes — no look-ahead within the same timeframe."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    out["roc_3"] = momentum(out["close"], p["roc_period"])
    out["rsi_6"] = ta.rsi(out["close"], length=p["rsi_period"]) - 50.0
    return out


def compute_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Entry/exit booleans replaying the original state machine as
    stateless per-bar conditions, plus a regime-dependent ``max_hold``
    column — position tracking (periods_held, side) stays in the Strategy
    layer.

    Range-mode (``is_ranging``): roc_3/rsi_6 extreme-reversal, exit when
    rsi_6 reverts near zero.
    Trend-mode (``~is_ranging``): funding-crowding confirms the daily
    momentum direction, exit when funding crowding fades.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    ranging, mom_up = out["is_ranging"], out["mom_up"]
    fz = out["funding_z_3d"]

    range_long = (out["roc_3"] < p["roc_buy"]) & (out["rsi_6"] < p["rsi_buy"])
    range_short = (out["roc_3"] > p["roc_sell"]) & (out["rsi_6"] > p["rsi_sell"])
    trend_long = mom_up & (fz > p["funding_z_long"])
    trend_short = (~mom_up) & (fz < p["funding_z_short"])

    out["long_entry"] = (ranging & range_long) | ((~ranging) & trend_long)
    out["short_entry"] = (ranging & range_short) | ((~ranging) & trend_short)

    range_exit = out["rsi_6"].abs() <= p["rsi_exit_band"]
    out["long_exit"] = (ranging & range_exit) | ((~ranging) & (fz < p["funding_exit_long"]))
    out["short_exit"] = (ranging & range_exit) | ((~ranging) & (fz > p["funding_exit_short"]))

    out["max_hold"] = ranging.map({True: p["max_hold_ranging"], False: p["max_hold_trending"]})
    return out


def prepare_signals(df_4h: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add regime gate + 4H features + entry/exit signals to a 4H OHLCV
    DataFrame that already has ``funding_z_3d`` attached (see
    ``strategies.module.data.funding.attach_funding_features`` — the I/O
    step stays in ``factor_research.py``). Expects DatetimeIndex.

    Handles short datasets (< mom_lookback + 5 daily bars) by skipping the
    gate (``is_ranging`` defaults True, i.e. range-mode-permitting)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    d1 = resample_ohlcv(df_4h, "1D")
    out = compute_4h_features(df_4h, params)
    if len(d1) >= p["mom_lookback"] + 5:
        d1_features = compute_daily_features(d1, params)
        out = merge_regime_gate(out, d1_features, params)
    else:
        out["is_ranging"] = True
        out["mom_up"] = True
    out = compute_signals(out, params)
    return out
