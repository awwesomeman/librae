#!/usr/bin/env python3
import os
import json
import pandas as pd
import numpy as np
import shioaji as sj


def indicators(df):
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


def resample(df, rule):
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()


def main():
    key = os.getenv("SINO_API_KEY")
    sec = os.getenv("SINO_SECRET_KEY")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    c = api.Contracts.Futures.MXF.MXFR1
    kb = api.kbars(c, start=os.getenv("SIG_START", "2026-01-01"), end=os.getenv("SIG_END", "2026-03-03"))
    df = pd.DataFrame({**kb})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.rename(columns={"Open":"open","High":"high","Low":"low","Close":"close","Volume":"volume"}).set_index("ts").sort_index()

    h1 = indicators(resample(df, "60min"))
    d1 = resample(df, "1D")
    d1["ema20"] = d1["close"].ewm(span=20, adjust=False).mean()
    d1["ema20_prev"] = d1["ema20"].shift(1)

    last = h1.iloc[-1]
    prev = h1.iloc[-2]
    day = last.name.floor("D")
    d = d1.loc[day]

    trend_long = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])
    near_ema = abs(last["low"] - last["ema20"]) <= 0.3 * last["atr14"]
    bullish = (last["close"] > last["open"]) and (last["close"] > prev["high"])
    vol_ok = last["volume"] >= 0.9 * last["vol_sma20"] if not np.isnan(last["vol_sma20"]) else False

    signal = trend_long and near_ema and bullish and vol_ok
    out = {
      "timestamp": last.name.isoformat(),
      "contract": "MXFR1",
      "signal": "LONG_ENTRY" if signal else "NO_SIGNAL",
      "checks": {
        "trend_long": bool(trend_long),
        "near_ema": bool(near_ema),
        "bullish_trigger": bool(bullish),
        "volume_ok": bool(vol_ok)
      }
    }

    if signal:
      stop = float(last["low"] - 0.2*last["atr14"])
      risk = float(last["close"] - stop)
      out["plan"] = {
        "entry": float(last["close"]),
        "stop": stop,
        "t1": float(last["close"] + 1.5*risk),
        "t2": float(last["close"] + 2.2*risk)
      }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    api.logout()

if __name__ == '__main__':
    main()
