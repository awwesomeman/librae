"""MTF Trend Slicing Regime — daily momentum trend gate + hourly RSI(14)
dip/rip timing, with an optional Fear & Greed / DXY sentiment filter gating
long entries only (asymmetric — matches the original research's placement:
shorts were never sentiment-gated).

Ported from ``strategies/experiments/mtf_trend_slicing_regime/`` (a different
project's ``utils/`` package, not runnable in this repo). Same daily-gate
no-lookahead ``shift(1)`` pattern as ``strategies/experiments/mtf_trend_rsi/utils.py``
and ``strategies/experiments/trendpullback/utils.py`` (see their docstrings/report.md
"先修 bug" section for the rationale) — a D1 bar's own value isn't fully known
until D1 closes, so the gate must be shifted by one bar before merging onto H1.

``fng_value``/``dxy_trend``/``vol_regime`` are expected to already be columns
on the input (attached by ``strategies.module.data.regime.attach_regime_columns``
in ``factor_research.py`` — an I/O call, kept out of this module) — the
sentiment gate here is a pure function of those existing columns, not a new
data fetch.

Pure functions: same input -> same output. No position tracking — that's
the Strategy layer's job.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.utils import merge_htf_column

DEFAULT_PARAMS: dict = {
    "rsi_period": 14,
    "buy_th": 30.0,
    "sell_th": 65.0,
    "short_th": 70.0,
    "cover_th": 35.0,
    "trend_lookback": 10,    # daily bars — mom_1D_10's own definition
    "gate_timeframe": "1D",
    "fng_min": 35.0,         # sentiment gate: fng_value >= fng_min
    "use_filter": True,      # False = ablation baseline, sentiment gate disabled
}


def compute_daily_trend_gate(df_gate: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """mom_1D_10 sign, via the shared ``momentum`` operator (also what
    ``factor_research.py``'s significance test computes — same formula,
    one definition)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return momentum(df_gate["close"], p["trend_lookback"]) > 0


def merge_daily_gate(detail: pd.DataFrame, gate_df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """No-lookahead merge — see module docstring. ``shift(1)`` makes a gate
    bar's index carry the *previous* gate bar's already-completed value.
    Defaults to ``True`` (bullish) before the first gate bar closes, same
    convention as ``mtf_trend_rsi``/``trendpullback``."""
    trend_bool = compute_daily_trend_gate(gate_df, params).shift(1)
    return merge_htf_column(detail, trend_bool, column="daily_trend_up", fill_value=True)


def compute_rsi(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    p = {**DEFAULT_PARAMS, **(params or {})}
    return ta.rsi(df["close"], length=p["rsi_period"])


def compute_sentiment_gate(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """Regime/sentiment filter for long entries only — bullish gate is
    "not too fearful, dollar not strongly trending up" (a strong dollar and
    extreme fear both historically correlate with crypto downside, per the
    original research). Expects ``fng_value``/``dxy_trend`` columns already
    attached (see module docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return (df["fng_value"] >= p["fng_min"]) & (df["dxy_trend"] != "strong_dxy")


def compute_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Entry/exit booleans replaying the original bull/bear state machine as
    stateless per-bar conditions — position tracking stays in the Strategy
    layer, which reads ``long_entry``/``short_entry`` only while flat and
    ``long_exit``/``short_exit`` only while in the matching side.

    Bull trend (``daily_trend_up``): longs only, on RSI dip; sentiment gate
    (``use_filter=True``) additionally requires FNG/DXY to be permissive —
    the original research's own asymmetric placement (never applied to
    shorts). Bear trend: shorts only, on RSI rip, never sentiment-gated.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    out["rsi"] = compute_rsi(out, params)

    long_rsi_ok = out["rsi"] < p["buy_th"]
    if p["use_filter"]:
        long_rsi_ok = long_rsi_ok & compute_sentiment_gate(out, params)

    out["long_entry"] = out["daily_trend_up"] & long_rsi_ok
    out["short_entry"] = (~out["daily_trend_up"]) & (out["rsi"] > p["short_th"])
    out["long_exit"] = out["rsi"] > p["sell_th"]
    out["short_exit"] = out["rsi"] < p["cover_th"]
    return out


def prepare_signals(h1_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add daily trend gate + RSI + entry/exit signals to an H1 OHLCV
    DataFrame. Expects DatetimeIndex + OHLCV columns, plus ``fng_value``/
    ``dxy_trend`` already attached (vol_regime is carried through untouched
    if present, used only for c slicing, not by the signals themselves).
    Handles short datasets (< trend_lookback + 5 gate-timeframe bars) by
    skipping the gate (``daily_trend_up`` defaults True)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    h1 = h1_base.copy()
    gate_df = resample_ohlcv(h1_base, p["gate_timeframe"])
    if len(gate_df) >= p["trend_lookback"] + 5:
        h1 = merge_daily_gate(h1, gate_df, params)
    else:
        h1["daily_trend_up"] = True
    h1 = compute_signals(h1, params)
    return h1
