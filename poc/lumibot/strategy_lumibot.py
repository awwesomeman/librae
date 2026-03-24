"""TrendPullback strategy implemented in Lumibot's framework.

Uses the same signal detection logic as the existing TrendPullback strategy,
but within Lumibot's event-driven backtesting engine. Pre-computed H1/D1
features are injected via parameters for PoC comparison.
"""
from __future__ import annotations

import uuid

import numpy as np
import pandas as pd

from lumibot.strategies import Strategy

from poc.lumibot.signal_schema import Signal


class TrendPullbackLumibot(Strategy):
    """Lumibot implementation of TrendPullback signal detection."""

    parameters = {
        "pull": 0.3,
        "bn": 5,
        "en": 20,
        "run_id": "",
        "_h1": None,
        "_d1": None,
        "_m1": None,
    }

    def initialize(self):
        self.signals: list[Signal] = []
        self._h1 = self.parameters.get("_h1")
        self._d1 = self.parameters.get("_d1")
        self._m1 = self.parameters.get("_m1")
        self._run_id = self.parameters.get("run_id") or uuid.uuid4().hex[:12]
        self._pull = self.parameters.get("pull", 0.3)
        self._bn = self.parameters.get("bn", 5)
        self._en = self.parameters.get("en", 20)
        self._processed_bars: set[pd.Timestamp] = set()
        self.sleeptime = "1H"

    def on_trading_iteration(self):
        """Process one H1 bar per iteration — detect TrendPullback entry signals."""
        if self._h1 is None or self._d1 is None or self._m1 is None:
            return

        dt = self.get_datetime()
        if dt is None:
            return

        current_ts = pd.Timestamp(dt)
        if current_ts.tz is None:
            current_ts = current_ts.tz_localize("UTC")
        else:
            current_ts = current_ts.tz_convert("UTC")

        h1_times = self._h1.index[self._h1.index <= current_ts]
        if len(h1_times) < 31:
            return

        t = h1_times[-1]
        if t in self._processed_bars:
            return
        self._processed_bars.add(t)

        all_h1_times = self._h1.index
        t_loc = all_h1_times.get_loc(t)
        if t_loc >= len(all_h1_times) - 1:
            return
        nt = all_h1_times[t_loc + 1]

        cur = self._h1.loc[t]
        if t_loc < 1:
            return
        prev = self._h1.iloc[t_loc - 1]

        day = t.floor("D") - pd.Timedelta(days=1)
        if day not in self._d1.index:
            return
        d = self._d1.loc[day]
        trend = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
        if not trend:
            return

        near = abs(cur["low"] - cur["ema20"]) <= self._pull * cur["atr14"]
        if not near:
            return

        bullish = (cur["close"] > cur["open"]) and (cur["close"] > prev["high"])
        if not bullish:
            return

        vol_ok = (
            (cur["volume"] >= 0.9 * cur["vol_sma20"])
            if not np.isnan(cur["vol_sma20"])
            else False
        )
        if not (vol_ok and cur["atr14"] > 0):
            return

        # Use searchsorted for O(log n) M1 slicing instead of boolean mask
        m1_idx = self._m1.index
        start_idx = m1_idx.searchsorted(t, side="right")
        end_idx = m1_idx.searchsorted(nt, side="right")
        ew = self._m1.iloc[start_idx:end_idx].copy()
        if len(ew) < max(self._en + 2, self._bn + 2):
            return
        ew["ema"] = ew["close"].ewm(span=self._en, adjust=False).mean()
        ew["hh"] = ew["high"].rolling(self._bn).max().shift(1)
        for ts, r in ew.iterrows():
            if np.isnan(r["ema"]) or np.isnan(r["hh"]):
                continue
            if r["close"] > r["hh"] and r["close"] > r["ema"]:
                self.signals.append(
                    Signal(
                        timestamp=ts,
                        side="long",
                        symbol="BTCUSDT",
                        strategy="TrendPullback_v1.0.0_lumibot",
                        run_id=self._run_id,
                    )
                )
                break
