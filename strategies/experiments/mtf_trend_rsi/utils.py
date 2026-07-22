"""MTF Trend RSI — daily momentum trend gate + hourly RSI(14) reversal.

Ported from ``strategies/experiments/mtf_trend_rsi/`` (a different project's
utils, not runnable in this repo — see that folder's README note) onto this
repo's actual tools. The daily gate + no-lookahead merge follows the same
pattern ``strategies/experiments/trendpullback/utils.py`` uses (and the same bug that
family had to fix — see its report.md's "先修 bug" section): a D1 bar's own
value isn't fully known until D1 closes, so the gate must be shifted by one
bar before merging onto H1.

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
    "trend_lookback": 10,   # daily bars — mom_1D_10's own definition
    "gate_timeframe": "1D",
    "use_gate": True,       # False = ablation baseline, both sides always allowed
}


def compute_daily_momentum_gate(df_gate: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """mom_1D_10 sign, via the shared ``momentum`` operator (also what
    ``factor_research.py``'s significance test computes — same formula,
    one definition)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return momentum(df_gate["close"], p["trend_lookback"]) > 0


def merge_daily_gate(detail: pd.DataFrame, gate_df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """No-lookahead merge — see module docstring. ``shift(1)`` makes a gate
    bar's index carry the *previous* gate bar's already-completed value."""
    trend_bool = compute_daily_momentum_gate(gate_df, params).shift(1)
    return merge_htf_column(detail, trend_bool, column="daily_trend_up", fill_value=False)


def compute_rsi(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    p = {**DEFAULT_PARAMS, **(params or {})}
    return ta.rsi(df["close"], length=p["rsi_period"])


def compute_signals(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Entry/exit booleans replaying the original state machine as
    stateless per-bar conditions — position tracking stays in the Strategy
    layer, which reads ``long_entry``/``short_entry`` only while flat and
    ``long_exit``/``short_exit`` only while in the matching side.

    ``use_gate=True`` (deployed logic): daily_trend_up sign gates which
    side may open — bullish gate allows longs only, bearish allows shorts
    only. ``use_gate=False`` (ablation baseline): both sides always
    allowed, pure RSI mean-reversion.
    """
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    out["rsi"] = compute_rsi(out, params)

    if p["use_gate"]:
        out["long_entry"] = out["daily_trend_up"] & (out["rsi"] < p["buy_th"])
        out["short_entry"] = (~out["daily_trend_up"]) & (out["rsi"] > p["short_th"])
    else:
        out["long_entry"] = out["rsi"] < p["buy_th"]
        out["short_entry"] = out["rsi"] > p["short_th"]

    out["long_exit"] = out["rsi"] > p["sell_th"]
    out["short_exit"] = out["rsi"] < p["cover_th"]
    return out


def prepare_signals(h1_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add daily trend gate + RSI + entry/exit signals to an H1 OHLCV
    DataFrame. Expects DatetimeIndex + OHLCV columns. Handles short
    datasets (< trend_lookback + 5 gate-timeframe bars) by skipping the
    gate (``daily_trend_up`` defaults True, i.e. long-only-permitting)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    h1 = h1_base.copy()
    gate_df = resample_ohlcv(h1_base, p["gate_timeframe"])
    if len(gate_df) >= p["trend_lookback"] + 5:
        h1 = merge_daily_gate(h1, gate_df, params)
    else:
        h1["daily_trend_up"] = True
    h1 = compute_signals(h1, params)
    return h1
