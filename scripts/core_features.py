#!/usr/bin/env python3
"""Reusable feature engineering for OHLCV time series."""
from __future__ import annotations

import pandas as pd
import numpy as np


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()


def add_trendpullback_features(df: pd.DataFrame) -> pd.DataFrame:
    o = df.copy()
    o["ema20"] = o["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        o["high"] - o["low"],
        (o["high"] - o["close"].shift(1)).abs(),
        (o["low"] - o["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    o["vol_sma20"] = o["volume"].rolling(20).mean()
    return o


def add_multifactor_features(df: pd.DataFrame) -> pd.DataFrame:
    o = add_trendpullback_features(df)
    o["ema60"] = o["close"].ewm(span=60, adjust=False).mean()
    o["ret20"] = o["close"].pct_change(20)
    o["ret60"] = o["close"].pct_change(60)
    o["atrp"] = o["atr14"] / o["close"]
    return o


def add_daily_trend_gate(df_1d: pd.DataFrame) -> pd.DataFrame:
    d = df_1d.copy()
    d["ema20"] = d["close"].ewm(span=20, adjust=False).mean()
    d["ema20_prev"] = d["ema20"].shift(1)
    return d


def multifactor_score(row: pd.Series) -> float:
    trend = 100 if (row["close"] > row["ema20"] > row["ema60"]) else (60 if row["close"] > row["ema20"] else 20)
    mom = 50 if (np.isnan(row["ret20"]) or np.isnan(row["ret60"])) else np.clip(50 + (0.6*row["ret20"] + 0.4*row["ret60"]) * 1000, 0, 100)
    volq = 40 if (np.isnan(row["vol_sma20"]) or row["vol_sma20"] <= 0) else np.clip((row["volume"] / row["vol_sma20"]) * 70, 0, 100)
    vp = 50 if np.isnan(row["atrp"]) else np.clip(100 - row["atrp"] * 5000, 0, 100)
    return float(0.35*trend + 0.30*mom + 0.20*volq + 0.15*vp)
