"""Range Oscillator utils — Keltner-channel mean-reversion features, Vol+Amp
consolidation filter, daily-momentum trend gate, OI-consolidating overlay.

Ported from ``range_oscillator_research.py`` (a different project's ``utils/``
package, not runnable in this repo) onto this repo's actual tools. The core
mean-reversion factor is Bollinger %b (demeaned around 0): when price sits
near/outside a Keltner channel edge, the strategy assumes it reverts toward
the mid band. The Vol+Amp filter only allows entries when recent
amplitude/volume are both below their own rolling baseline ("consolidating").
The daily trend gate (mom_1D_10 sign, same no-lookahead shift(1) pattern
``trendpullback/utils.py`` and ``mtf_trend_rsi/utils.py`` use) only allows
longs in an up-trend day and shorts in a down-trend day. The OI filter
requires |OI 24h change| below a threshold on top of Vol+Amp.

Pure functions: same input -> same output. No position tracking — that's
the Strategy layer's job.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta_classic as ta

from strategies.module.data.open_interest import attach_oi_features
from strategies.module.data.utils import resample_ohlcv
from strategies.module.factors.operators import momentum
from strategies.module.utils import merge_htf_column

DEFAULT_PARAMS: dict = {
    "bb_period": 20,
    "atr_period": 14,
    "keltner_mult": 1.5,
    "amp_window": 24,
    "vol_window": 24,
    "amp_mult": 1.2,
    "vol_mult": 1.3,
    "trend_lookback": 10,   # daily bars — mom_1D_10's own definition
    "gate_timeframe": "1D",
    "oi_threshold": 5.0,    # |OI 24h change| (%) below this = "OI-consolidating"
    "use_vol_amp_filter": True,
    "use_trend_filter": True,
    "consolidating_col": "is_consolidating",
}


# ---------------------------------------------------------------------------
# Feature computation (pure)
# ---------------------------------------------------------------------------

def compute_features(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Keltner channel (mid/upper/lower) + Bollinger %b (demeaned) + Vol+Amp
    consolidation filter. Required columns: open, high, low, close, volume."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]

    out["mid"] = close.rolling(p["bb_period"]).mean()
    out["atr"] = ta.atr(high, low, close, length=p["atr_period"])
    out["upper"] = out["mid"] + p["keltner_mult"] * out["atr"]
    out["lower"] = out["mid"] - p["keltner_mult"] * out["atr"]

    bb_std = close.rolling(p["bb_period"]).std()
    bb_lower = out["mid"] - 2 * bb_std
    bb_upper = out["mid"] + 2 * bb_std
    out["bb_pct_b"] = (close - bb_lower) / (bb_upper - bb_lower) - 0.5

    out["amp"] = (high - low) / (close + 1e-9)
    out["amp_sma"] = out["amp"].rolling(p["amp_window"]).mean()
    out["vol_sma"] = vol.rolling(p["vol_window"]).mean()
    out["is_consolidating"] = (out["amp"] < out["amp_sma"] * p["amp_mult"]) & (vol < out["vol_sma"] * p["vol_mult"])

    return out


def merge_daily_trend(detail: pd.DataFrame, h1_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Merge daily mom_1D_10 (signed, continuous) onto H1 — no look-ahead:
    a D1 bar's own value isn't fully known until D1 closes, so the gate is
    shifted by one bar before the backward asof-merge (same fix
    ``trendpullback``/``mtf_trend_rsi`` needed, see their report.md's "先修
    bug" sections)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    gate_df = resample_ohlcv(h1_base, p["gate_timeframe"])
    out = detail.copy()
    if len(gate_df) < p["trend_lookback"] + 5:
        out["daily_mom"] = 0.0
        return out
    daily_mom = momentum(gate_df["close"], p["trend_lookback"]).shift(1)
    return merge_htf_column(out, daily_mom, column="daily_mom", fill_value=0.0)


def attach_oi_regime(df: pd.DataFrame, symbol: str, start: str, end: str, params: dict | None = None) -> pd.DataFrame:
    """Attach ``oi_consolidating``/``is_consolidating_oi`` (Vol+Amp AND OI).
    Requires ``is_consolidating`` already present (see ``compute_features``).
    Rows without OI coverage are dropped — an absent OI reading means the OI
    leg of the filter can't be evaluated for that row, not that it should
    fall back to "always true"."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.reset_index()
    ts_col = out.columns[0]
    out = out.rename(columns={ts_col: "timestamp"})
    out = attach_oi_features(out, symbol, start, end).dropna(subset=["open_interest_change_24h"])
    out["oi_consolidating"] = out["open_interest_change_24h"].abs() < p["oi_threshold"]
    out["is_consolidating_oi"] = out["is_consolidating"] & out["oi_consolidating"]
    return out.set_index("timestamp")


# ---------------------------------------------------------------------------
# Signal conditions (pure boolean Series — no position tracking)
# ---------------------------------------------------------------------------

def compute_signal_conditions(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Add long_entry/short_entry/long_exit/short_exit. Entries require
    (optionally) the Vol+Amp/OI consolidation filter AND (optionally) the
    daily trend gate; exits are unconditional mid-band crossings — matches
    the legacy strategy's flat state machine, replayed as stateless per-bar
    conditions (position tracking stays in the Strategy layer)."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = df.copy()

    if p["use_vol_amp_filter"]:
        ok_to_trade = out[p["consolidating_col"]]
    else:
        ok_to_trade = pd.Series(True, index=out.index)

    if p["use_trend_filter"]:
        trend_up = out["daily_mom"] > 0
        trend_down = out["daily_mom"] < 0
    else:
        trend_up = pd.Series(True, index=out.index)
        trend_down = pd.Series(True, index=out.index)

    out["long_entry"] = (ok_to_trade & trend_up & (out["close"] < out["lower"])).fillna(False)
    out["short_entry"] = (ok_to_trade & trend_down & (out["close"] > out["upper"])).fillna(False)
    out["long_exit"] = (out["close"] > out["mid"]).fillna(False)
    out["short_exit"] = (out["close"] < out["mid"]).fillna(False)
    return out


def prepare_signals(h1_base: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    """Full pipeline for the non-OI candidates: features + daily trend gate
    + entry/exit signals. Expects DatetimeIndex + OHLCV columns."""
    p = {**DEFAULT_PARAMS, **(params or {})}
    out = compute_features(h1_base, params)
    out = merge_daily_trend(out, h1_base, params)
    out = compute_signal_conditions(out, p)
    return out
