#!/usr/bin/env python3
import os
import json
import subprocess
from pathlib import Path

import pandas as pd
import numpy as np
import shioaji as sj

STATE_PATH = Path("/home/jasonpan_subscribe/.openclaw/workspace/data/shioaji/mxf_signal_state.json")


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


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"last_signal_ts": None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    key = os.getenv("SINO_API_KEY")
    sec = os.getenv("SINO_SECRET_KEY")
    if not key or not sec:
        print("Missing SINO_API_KEY/SINO_SECRET_KEY")
        return 1

    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)

    c = api.Contracts.Futures.MXF.MXFR1
    kb = api.kbars(c, start=os.getenv("SIG_START", "2026-01-01"), end=os.getenv("SIG_END", "2026-12-31"))
    df = pd.DataFrame({**kb})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}).set_index("ts").sort_index()

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

    signal = bool(trend_long and near_ema and bullish and vol_ok)
    ts = last.name.isoformat()

    state = load_state()
    if signal and state.get("last_signal_ts") != ts:
        stop = float(last["low"] - 0.2 * last["atr14"])
        risk = float(last["close"] - stop)
        t1 = float(last["close"] + 1.5 * risk)
        t2 = float(last["close"] + 2.2 * risk)

        text = (
            f"MXF 訊號觸發（保守版）\n"
            f"時間: {ts}\n"
            f"方向: LONG_ENTRY\n"
            f"進場: {float(last['close']):.1f}\n"
            f"停損: {stop:.1f}\n"
            f"停利1: {t1:.1f}\n"
            f"停利2: {t2:.1f}\n"
            f"邏輯: 日線趨勢多 + 60m回踩EMA + 多方觸發 + 量能確認"
        )
        subprocess.run(["openclaw", "system", "event", "--text", text, "--mode", "now"], check=False)
        state["last_signal_ts"] = ts
        save_state(state)

    api.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
