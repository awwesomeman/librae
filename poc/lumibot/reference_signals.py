"""Extract entry signals from the existing TrendPullback strategy.

Reuses the exact same signal detection logic from the existing
strategy_trendpullback_v1_0_0_h1_l_btcf.py, but records each entry signal
in a standardized format instead of tracking positions.
"""
from __future__ import annotations

import uuid

import numpy as np
import pandas as pd

from poc.lumibot.signal_schema import Signal


def extract_signals(
    m1: pd.DataFrame,
    h1: pd.DataFrame,
    d1: pd.DataFrame,
    start: str,
    end: str,
    *,
    pull: float = 0.3,
    bn: int = 5,
    en: int = 20,
    run_id: str | None = None,
) -> list[Signal]:
    """Extract entry signals using the existing TrendPullback logic."""
    if run_id is None:
        run_id = uuid.uuid4().hex[:12]

    h = h1[(h1.index >= start) & (h1.index <= end)]
    signals: list[Signal] = []

    for i in range(30, len(h) - 1):
        cur = h.iloc[i]
        prev = h.iloc[i - 1]
        t = h.index[i]
        nt = h.index[i + 1]

        day = t.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
        if not trend:
            continue

        near = abs(cur["low"] - cur["ema20"]) <= pull * cur["atr14"]
        if not near:
            continue

        bullish = (cur["close"] > cur["open"]) and (cur["close"] > prev["high"])
        if not bullish:
            continue

        vol_ok = (
            (cur["volume"] >= 0.9 * cur["vol_sma20"])
            if not np.isnan(cur["vol_sma20"])
            else False
        )
        if not (vol_ok and cur["atr14"] > 0):
            continue

        ew = m1[(m1.index > t) & (m1.index <= nt)].copy()
        if len(ew) < max(en + 2, bn + 2):
            continue
        ew["ema"] = ew["close"].ewm(span=en, adjust=False).mean()
        ew["hh"] = ew["high"].rolling(bn).max().shift(1)
        for ts, r in ew.iterrows():
            if np.isnan(r["ema"]) or np.isnan(r["hh"]):
                continue
            if r["close"] > r["hh"] and r["close"] > r["ema"]:
                signals.append(
                    Signal(
                        timestamp=ts,
                        side="long",
                        symbol="BTCUSDT",
                        strategy="TrendPullback_v1.0.0_reference",
                        run_id=run_id,
                    )
                )
                break

    return signals
