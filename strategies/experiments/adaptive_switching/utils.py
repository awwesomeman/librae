"""Adaptive Switching — daily macro-trend gate + regime-conditional sub-signal
selection between a momentum (breakout) sub-strategy and an RSI (mean-
reversion) sub-strategy.

Ported from a different project's ``utils/`` (not runnable in this repo) onto
this repo's actual tools. The original regime switch was a bespoke "cumulative
intraday volume vs 30-day hour-of-day average" ratio (``vol_ratio > 1.15``),
specific to a 24/7-market assumption and not reusable elsewhere. This version
uses the shared ``strategies.module.data.regime.compute_vol_regime`` (ATR-vs-
its-own-rolling-baseline high_vol/low_vol classifier) instead — same
hypothesis (switch sub-strategy by a volatility-regime read), different (already
shared, no-look-ahead) regime definition. See factor_research.py / report.md
for why this substitution was made explicit rather than silent.

The daily macro-trend gate (``mom_1D_10`` sign) is the same definition and
same no-look-ahead ``shift(1)`` + ``merge_htf_column`` pattern as
``strategies/experiments/mtf_trend_rsi/utils.py`` uses for its own gate — both
families gate direction (long-only vs short-only) on the daily trend.

Pure functions: same input -> same output. No position tracking — that's the
Strategy layer's job.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from strategies.module.data.regime import compute_vol_regime
from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.utils import merge_htf_column

DEFAULT_PARAMS: dict = {
    "rsi_period": 14,
    "buy_th": 30.0, "sell_th": 65.0, "short_th": 70.0, "cover_th": 35.0,
    "mom_lookback": 12,          # mom_1H_12, same definition as the legacy script
    "mom_entry_th": 0.005,
    "mom_exit_th": 0.002,
    "trend_lookback": 10,        # mom_1D_10 macro gate, same def as mtf_trend_rsi
    "gate_timeframe": "1D",
    "vol_window": 14, "vol_baseline": 120,  # compute_vol_regime's own defaults
}


def compute_daily_momentum_gate(df_gate: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """mom_1D_10 sign — identical definition to
    ``mtf_trend_rsi/utils.py::compute_daily_momentum_gate``."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return momentum(df_gate["close"], p["trend_lookback"]) > 0


def merge_daily_gate(detail: pd.DataFrame, gate_df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """No-lookahead merge: ``shift(1)`` so a gate bar's index carries the
    *previous* gate bar's already-completed value (see
    ``trendpullback/utils.py::merge_trend_gate`` docstring for the full
    look-ahead rationale this pattern fixes)."""
    trend_bool = compute_daily_momentum_gate(gate_df, params).shift(1)
    return merge_htf_column(detail, trend_bool, column="daily_trend_up", fill_value=False)


def compute_features(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """RSI(14) + mom_1H_12, the two sub-signal factors."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    out["rsi"] = ta.rsi(out["close"], length=p["rsi_period"])
    out["mom_1h"] = momentum(out["close"], p["mom_lookback"])
    return out


def compute_vol_regime_column(df: pd.DataFrame, params: dict | None = None) -> pd.Series:
    """high_vol/low_vol via the shared regime classifier — no look-ahead
    (expanding-safe rolling baseline, see ``regime.py``'s own docstring)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    return compute_vol_regime(df, window=p["vol_window"], baseline=p["vol_baseline"])


def compute_momentum_signals(df: pd.DataFrame, params: dict | None = None) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Momentum (breakout) sub-strategy — direction gated by the daily macro
    trend, entry/exit thresholds on mom_1H_12 (same thresholds as the legacy
    script's trend-regime branch: entry 0.5%, exit -0.2% for longs, mirrored
    for shorts)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    long_entry = (df["daily_trend_up"] & (df["mom_1h"] > p["mom_entry_th"])).fillna(False)
    long_exit = (df["mom_1h"] < -p["mom_exit_th"]).fillna(False)
    short_entry = ((~df["daily_trend_up"]) & (df["mom_1h"] < -p["mom_entry_th"])).fillna(False)
    short_exit = (df["mom_1h"] > p["mom_exit_th"]).fillna(False)
    return long_entry, long_exit, short_entry, short_exit


def compute_rsi_signals(df: pd.DataFrame, params: dict | None = None) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """RSI mean-reversion sub-strategy — direction gated by the daily macro
    trend, same thresholds as the legacy script's range-regime branch and as
    ``mtf_trend_rsi/utils.py::compute_signals``."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    long_entry = (df["daily_trend_up"] & (df["rsi"] < p["buy_th"])).fillna(False)
    long_exit = (df["rsi"] > p["sell_th"]).fillna(False)
    short_entry = ((~df["daily_trend_up"]) & (df["rsi"] > p["short_th"])).fillna(False)
    short_exit = (df["rsi"] < p["cover_th"]).fillna(False)
    return long_entry, long_exit, short_entry, short_exit


def compute_switching_signals(df: pd.DataFrame, params: dict | None = None) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Per-bar select momentum sub-signals in high_vol, RSI sub-signals in
    low_vol — the adaptive-switching hypothesis: momentum breakout should work
    when volatility is elevated, mean-reversion when it's subdued."""
    mom = compute_momentum_signals(df, params)
    rsi = compute_rsi_signals(df, params)
    is_high_vol = (df["vol_regime"] == "high_vol").values
    return tuple(
        pd.Series(np.where(is_high_vol, m.values, r.values), index=df.index)
        for m, r in zip(mom, rsi)
    )


def prepare_signals(h1_base: pd.DataFrame, params: dict | None = None, mode: str = "switch") -> pd.DataFrame:
    """Add daily macro gate + vol_regime + RSI/momentum + entry/exit signals
    to an H1 OHLCV DataFrame. ``mode``: "switch" (deployed adaptive logic),
    "momentum" (always use the momentum sub-strategy — the ``always_trend``
    ablation in the legacy script), "rsi" (always use the RSI sub-strategy —
    the ``always_range`` ablation). Expects DatetimeIndex + OHLCV columns.
    Handles short datasets (< trend_lookback + 5 gate-timeframe bars) by
    skipping the gate (``daily_trend_up`` defaults True)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    h1 = compute_features(h1_base, params)
    gate_df = resample_ohlcv(h1_base, p["gate_timeframe"])
    if len(gate_df) >= p["trend_lookback"] + 5:
        h1 = merge_daily_gate(h1, gate_df, params)
    else:
        h1["daily_trend_up"] = True
    h1["vol_regime"] = compute_vol_regime_column(h1, params)

    if mode == "momentum":
        signals = compute_momentum_signals(h1, params)
    elif mode == "rsi":
        signals = compute_rsi_signals(h1, params)
    else:
        signals = compute_switching_signals(h1, params)
    h1["long_entry"], h1["long_exit"], h1["short_entry"], h1["short_exit"] = signals
    return h1
