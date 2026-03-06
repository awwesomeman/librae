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
    round_trip_cost_points: float = 2.0


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift(1)).abs(),
            (out["low"] - out["close"].shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = tr.rolling(14).mean()
    out["vol_sma20"] = out["volume"].rolling(20).mean()
    return out


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    agg = df.resample(freq).agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return agg.dropna()


def run_trendpullback_backtest(m1: pd.DataFrame, params: TrendPullBackParams) -> tuple[list[dict], np.ndarray]:
    h1 = add_features(resample_ohlcv(m1, "60min"))
    d1 = add_features(resample_ohlcv(m1, "1D"))
    d1["ema20_prev"] = d1["ema20"].shift(1)

    trades: list[dict] = []
    rets: list[float] = []
    pos = None

    for i in range(max(40, params.ema_span + params.breakout_lookback_bars + 5), len(h1) - 1):
        t = h1.index[i]
        cur = h1.iloc[i]
        if pos is not None:
            w = m1[(m1.index > pos["last_ts"]) & (m1.index <= t)]
            closed = False
            for ts, r in w.iterrows():
                if r["low"] <= pos["stop"]:
                    ret = (pos["stop"] - pos["entry_price"]) / pos["entry_price"] - params.round_trip_cost_points / pos["entry_price"]
                    rets.append(ret)
                    trades.append({"entry_ts": pos["entry_ts"], "exit_ts": ts, "entry_price": pos["entry_price"], "exit_price": pos["stop"], "ret": ret, "holding_bars": pos["bars"]})
                    pos = None
                    closed = True
                    break
                if r["high"] >= pos["target"]:
                    ret = (pos["target"] - pos["entry_price"]) / pos["entry_price"] - params.round_trip_cost_points / pos["entry_price"]
                    rets.append(ret)
                    trades.append({"entry_ts": pos["entry_ts"], "exit_ts": ts, "entry_price": pos["entry_price"], "exit_price": pos["target"], "ret": ret, "holding_bars": pos["bars"]})
                    pos = None
                    closed = True
                    break
            if not closed and pos is not None:
                pos["bars"] += 1
                pos["last_ts"] = t
                if pos["bars"] >= params.time_stop_bars:
                    ret = (cur["close"] - pos["entry_price"]) / pos["entry_price"] - params.round_trip_cost_points / pos["entry_price"]
                    rets.append(ret)
                    trades.append({"entry_ts": pos["entry_ts"], "exit_ts": t, "entry_price": pos["entry_price"], "exit_price": float(cur["close"]), "ret": ret, "holding_bars": pos["bars"]})
                    pos = None

        if pos is not None:
            continue

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

        hs = h1.iloc[max(0, i - params.ema_span - params.breakout_lookback_bars - 5): i + 1]
        ema_h1 = float(hs["close"].ewm(span=params.ema_span, adjust=False).mean().iloc[-1])
        hh_h1 = float(hs["high"].rolling(params.breakout_lookback_bars).max().shift(1).iloc[-1])
        if np.isnan(ema_h1) or np.isnan(hh_h1):
            continue
        if not (cur["close"] > ema_h1 and cur["close"] > hh_h1):
            continue

        entry = float(cur["close"])
        stop = float(d["low"] - 0.2 * d["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {
            "entry_ts": t,
            "entry_price": entry,
            "stop": stop,
            "target": entry + 2.2 * risk,
            "bars": 0,
            "last_ts": t,
        }

    return trades, np.asarray(rets, dtype=float)
