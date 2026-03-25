"""TrendPullBack strategy core for Binance BTC.

Single strategy definition used by both the backtest runner and the sim-live
signal runner. No execution logic — only signal generation.
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

    # Pre-compute vectorized indicators to avoid per-bar EWM/rolling (hot-path fix)
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


# ---------------------------------------------------------------------------
# Backtest engine (used by backtest runner)
# ---------------------------------------------------------------------------


def backtest(
    m1: pd.DataFrame,
    h1: pd.DataFrame,
    d1: pd.DataFrame,
    params: TrendPullBackParams,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    """Run full backtest, return legacy-compatible metrics dict with trade_details."""
    if start:
        h1_filtered = h1[h1.index >= start]
    else:
        h1_filtered = h1
    if end:
        h1_filtered = h1_filtered[h1_filtered.index <= end]

    cost_ratio = params.round_trip_cost_bps / 10000
    trades: list[dict] = []
    rets: list[float] = []
    pos = None
    warmup = max(40, params.ema_span + params.breakout_lookback_bars + 5)

    # Pre-compute vectorized indicators (hot-path fix)
    bt_ema = h1_filtered["close"].ewm(span=params.ema_span, adjust=False).mean()
    bt_hh = h1_filtered["high"].rolling(params.breakout_lookback_bars).max().shift(1)

    for i in range(warmup, len(h1_filtered) - 1):
        t = h1_filtered.index[i]
        cur = h1_filtered.iloc[i]

        if pos is not None:
            w = m1[(m1.index > pos["last_ts"]) & (m1.index <= t)]
            closed = False
            for ts, r in w.iterrows():
                if r["low"] <= pos["stop"]:
                    ret = (pos["stop"] - pos["entry_price"]) / pos["entry_price"] - cost_ratio
                    rets.append(ret)
                    trades.append({"entry_ts": str(pos["entry_ts"]), "exit_ts": str(ts), "entry_price": pos["entry_price"], "exit_price": pos["stop"], "pnl_return": ret, "bars_held": pos["bars"]})
                    pos = None; closed = True; break
                if r["high"] >= pos["target"]:
                    ret = (pos["target"] - pos["entry_price"]) / pos["entry_price"] - cost_ratio
                    rets.append(ret)
                    trades.append({"entry_ts": str(pos["entry_ts"]), "exit_ts": str(ts), "entry_price": pos["entry_price"], "exit_price": pos["target"], "pnl_return": ret, "bars_held": pos["bars"]})
                    pos = None; closed = True; break

            if not closed and pos is not None:
                pos["bars"] += 1
                pos["last_ts"] = t
                if pos["bars"] >= params.time_stop_bars:
                    ret = (float(cur["close"]) - pos["entry_price"]) / pos["entry_price"] - cost_ratio
                    rets.append(ret)
                    trades.append({"entry_ts": str(pos["entry_ts"]), "exit_ts": str(t), "entry_price": pos["entry_price"], "exit_price": float(cur["close"]), "pnl_return": ret, "bars_held": pos["bars"]})
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
        near = abs(d["low"] - d["ema20"]) <= params.pullback_atr_ratio * d["atr14"]
        vol_ok = d["volume"] >= 0.9 * d.get("vol_sma20", np.inf)
        if not (trend_ok and near and vol_ok):
            continue

        ema_val = bt_ema.iloc[i]
        hh_val = bt_hh.iloc[i]
        if np.isnan(ema_val) or np.isnan(hh_val):
            continue
        if not (cur["close"] > ema_val and cur["close"] > hh_val):
            continue

        entry = float(cur["close"])
        stop = float(d["low"] - 0.2 * d["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {
            "entry_ts": t, "entry_price": entry,
            "stop": stop, "target": entry + 2.2 * risk,
            "bars": 0, "last_ts": t,
        }

    if not rets:
        return {"trades": 0, "ann_return": 0, "ann_sharpe": 0, "mdd": 0, "pf": 0,
                "win_rate": 0, "avg_ret": 0, "avg_pnl_points": 0, "equity": 1.0, "trade_details": []}

    arr = np.array(rets)
    wins = int((arr > 0).sum())
    gp = float(arr[arr > 0].sum()) if wins > 0 else 0.0
    gl = float(-arr[arr < 0].sum()) if (arr < 0).any() else 0.0
    pf = gp / gl if gl > 0 else float("inf")
    eq = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(eq)
    mdd = float(np.max((peak - eq) / peak))

    s_start = start or str(h1_filtered.index[0])
    s_end = end or str(h1_filtered.index[-1])
    years = max((pd.Timestamp(s_end) - pd.Timestamp(s_start)).days / 365.25, 1e-6)
    ann = float(eq[-1] ** (1 / years) - 1)
    tpy = len(arr) / years
    vol = float(arr.std(ddof=1) * np.sqrt(tpy)) if len(arr) > 1 else 0.0
    sharpe = float((arr.mean() / arr.std(ddof=1)) * np.sqrt(tpy)) if len(arr) > 1 and arr.std(ddof=1) > 0 else 0.0
    pnl_pts = [t["exit_price"] - t["entry_price"] for t in trades]

    return {
        "trades": len(arr),
        "win_rate": wins / len(arr),
        "avg_ret": float(arr.mean()),
        "pf": pf,
        "ann_return": ann,
        "ann_sharpe": sharpe,
        "ann_vol": vol,
        "mdd": mdd,
        "equity": float(eq[-1]),
        "avg_pnl_points": float(np.mean(pnl_pts)) if pnl_pts else 0.0,
        "trade_details": trades,
    }
