"""TrendPullBack strategy definition for Binance BTC.

Pure strategy definition: parameters + signal generation.
No execution logic — use quant_lab.backtest.engine for backtesting.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class TrendPullBackParams:
    pullback_atr_ratio: float = 0.30
    breakout_lookback_bars: int = 5
    ema_span: int = 20
    time_stop_bars: int = 6
    round_trip_cost_bps: float = 8.0


# ---------------------------------------------------------------------------
# Feature engineering (pure functions, no side effects)
# ---------------------------------------------------------------------------


def add_h1_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift(1)).abs(),
        (out["low"] - out["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["vol_sma20"] = out["volume"].rolling(20).mean()
    return out


def add_daily_gate(df: pd.DataFrame) -> pd.DataFrame:
    out = add_h1_features(df)
    out["ema20_prev"] = out["ema20"].shift(1)
    return out


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    agg = df.resample(freq).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    })
    return agg.dropna()


# ---------------------------------------------------------------------------
# Signal generation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Signal:
    ts: pd.Timestamp
    side: str          # "buy"
    entry_price: float
    stop_price: float
    target_price: float
    strength: float    # 0-1 confidence


def generate_signals(
    m1: pd.DataFrame,
    h1: pd.DataFrame,
    d1: pd.DataFrame,
    params: TrendPullBackParams,
    start: str | None = None,
    end: str | None = None,
) -> list[Signal]:
    """Generate entry signals from OHLCV data. Pure function, no execution."""
    if start:
        h1 = h1[h1.index >= start]
    if end:
        h1 = h1[h1.index <= end]

    signals: list[Signal] = []
    warmup = max(40, params.ema_span + params.breakout_lookback_bars + 5)

    h1_ema = h1["close"].ewm(span=params.ema_span, adjust=False).mean()
    h1_hh = h1["high"].rolling(params.breakout_lookback_bars).max().shift(1)

    for i in range(warmup, len(h1)):
        t = h1.index[i]
        cur = h1.iloc[i]

        day = t.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        if pd.isna(d.get("atr14")) or d["atr14"] <= 0:
            continue

        trend_ok = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
        near_pullback = abs(d["low"] - d["ema20"]) <= params.pullback_atr_ratio * d["atr14"]
        vol_ok = d["volume"] >= 0.9 * d.get("vol_sma20", np.inf)
        if not (trend_ok and near_pullback and vol_ok):
            continue

        ema_val = h1_ema.iloc[i]
        hh_val = h1_hh.iloc[i]
        if np.isnan(ema_val) or np.isnan(hh_val):
            continue
        if not (cur["close"] > ema_val and cur["close"] > hh_val):
            continue

        entry = float(cur["close"])
        stop = float(d["low"] - 0.2 * d["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue

        strength = min(1.0, risk / (entry * 0.03))
        signals.append(Signal(
            ts=t,
            side="buy",
            entry_price=entry,
            stop_price=stop,
            target_price=entry + 2.2 * risk,
            strength=strength,
        ))

    return signals
