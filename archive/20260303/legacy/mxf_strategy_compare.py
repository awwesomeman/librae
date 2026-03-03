#!/usr/bin/env python3
import os
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import shioaji as sj


def resample_ohlc(df, rule):
    ohlc = pd.DataFrame()
    ohlc["open"] = df["open"].resample(rule).first()
    ohlc["high"] = df["high"].resample(rule).max()
    ohlc["low"] = df["low"].resample(rule).min()
    ohlc["close"] = df["close"].resample(rule).last()
    ohlc["volume"] = df["volume"].resample(rule).sum()
    return ohlc.dropna()


def add_indicators(df):
    out = df.copy()
    out["ema20"] = out["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift(1)).abs(),
        (out["low"] - out["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    out["vol_sma20"] = out["volume"].rolling(20).mean()
    return out


@dataclass
class Trade:
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    reason: str
    ret: float


def summarize(trades):
    if not trades:
        return {"trades": 0}
    rets = np.array([t.ret for t in trades])
    wins = int((rets > 0).sum())
    gross_p = float(rets[rets > 0].sum())
    gross_l = float(-rets[rets < 0].sum())
    pf = gross_p / gross_l if gross_l > 0 else None

    eq = 1.0
    peak = 1.0
    mdd = 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)

    return {
        "trades": int(len(rets)),
        "win_rate": float(wins / len(rets)),
        "avg_ret_per_trade": float(rets.mean()),
        "profit_factor": float(pf) if pf is not None else None,
        "mdd": float(mdd),
        "equity_multiple": float(eq),
    }


def long_setup(h1_row, h1_prev, d1_row):
    trend_long = (d1_row["close"] > d1_row["ema20"]) and (d1_row["ema20"] > d1_row["ema20_prev"])
    near_ema = abs(h1_row["low"] - h1_row["ema20"]) <= 0.3 * h1_row["atr14"]
    bullish = (h1_row["close"] > h1_row["open"]) and (h1_row["close"] > h1_prev["high"])
    vol_ok = h1_row["volume"] >= 0.9 * h1_row["vol_sma20"] if not np.isnan(h1_row["vol_sma20"]) else False
    return trend_long and near_ema and bullish and vol_ok


def backtest_v1(h1, d1):
    trades = []
    pos = None
    for i in range(30, len(h1)):
        row = h1.iloc[i]
        prev = h1.iloc[i - 1]
        day = row.name.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]

        if pos is not None:
            pos["bars"] += 1
            if row["low"] <= pos["stop"]:
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], pos["stop"], "stop", (pos["stop"] - pos["entry"]) / pos["entry"]))
                pos = None
                continue
            if (not pos["t1_done"]) and row["high"] >= pos["t1"]:
                pos["t1_done"] = True
                pos["partial"] = 0.5 * ((pos["t1"] - pos["entry"]) / pos["entry"])
            if row["high"] >= pos["t2"]:
                remain = 0.5 if pos["t1_done"] else 1.0
                total = pos["partial"] + remain * ((pos["t2"] - pos["entry"]) / pos["entry"])
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], pos["t2"], "target", total))
                pos = None
                continue
            if (pos["bars"] >= 6) or (row["close"] < row["ema20"]):
                remain = 0.5 if pos["t1_done"] else 1.0
                total = pos["partial"] + remain * ((row["close"] - pos["entry"]) / pos["entry"])
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], row["close"], "time_or_ema", total))
                pos = None
                continue

        if pos is None and long_setup(row, prev, d) and row["atr14"] > 0:
            stop = row["low"] - 0.2 * row["atr14"]
            risk = row["close"] - stop
            if risk <= 0:
                continue
            pos = {
                "entry_time": row.name.isoformat(),
                "entry": float(row["close"]),
                "stop": float(stop),
                "t1": float(row["close"] + 1.5 * risk),
                "t2": float(row["close"] + 2.2 * risk),
                "bars": 0,
                "t1_done": False,
                "partial": 0.0,
            }

    return trades


def backtest_v2_intra(h1, d1, m1):
    """60m setup + 在下一個60分鐘內用1m突破找進場點"""
    trades = []
    pos = None

    # 預先建立60m對應的1m切片
    for i in range(30, len(h1) - 1):
        cur = h1.iloc[i]
        prev = h1.iloc[i - 1]
        next_bar_time = h1.index[i + 1]
        cur_time = h1.index[i]

        day = cur.name.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]

        # 先處理既有部位，使用1m資料精細出場
        if pos is not None:
            window = m1[(m1.index > pos["last_check"]) & (m1.index <= cur_time)]
            for ts, r in window.iterrows():
                if r["low"] <= pos["stop"]:
                    trades.append(Trade(pos["entry_time"], ts.isoformat(), pos["entry"], pos["stop"], "stop", (pos["stop"] - pos["entry"]) / pos["entry"]))
                    pos = None
                    break
                if (not pos["t1_done"]) and r["high"] >= pos["t1"]:
                    pos["t1_done"] = True
                    pos["partial"] = 0.5 * ((pos["t1"] - pos["entry"]) / pos["entry"])
                if r["high"] >= pos["t2"]:
                    remain = 0.5 if pos["t1_done"] else 1.0
                    total = pos["partial"] + remain * ((pos["t2"] - pos["entry"]) / pos["entry"])
                    trades.append(Trade(pos["entry_time"], ts.isoformat(), pos["entry"], pos["t2"], "target", total))
                    pos = None
                    break
            if pos is not None:
                pos["bars"] += 1
                if (pos["bars"] >= 6) or (cur["close"] < cur["ema20"]):
                    remain = 0.5 if pos["t1_done"] else 1.0
                    total = pos["partial"] + remain * ((cur["close"] - pos["entry"]) / pos["entry"])
                    trades.append(Trade(pos["entry_time"], cur_time.isoformat(), pos["entry"], cur["close"], "time_or_ema", total))
                    pos = None
                else:
                    pos["last_check"] = cur_time

        if pos is not None:
            continue

        # 新進場：60m符合後，在下一根60m內用1m觸發
        if not long_setup(cur, prev, d) or cur["atr14"] <= 0:
            continue

        entry_window = m1[(m1.index > cur_time) & (m1.index <= next_bar_time)]
        if entry_window.empty:
            continue

        # 1m trigger: 價格突破前5根1m高點 + 站上1m EMA20
        m1_local = entry_window.copy()
        m1_local["ema20"] = m1_local["close"].ewm(span=20, adjust=False).mean()
        m1_local["hh5"] = m1_local["high"].rolling(5).max().shift(1)

        trigger_row = None
        for ts, r in m1_local.iterrows():
            if np.isnan(r["hh5"]) or np.isnan(r["ema20"]):
                continue
            if (r["close"] > r["hh5"]) and (r["close"] > r["ema20"]):
                trigger_row = (ts, r)
                break

        if trigger_row is None:
            continue

        ts, r = trigger_row
        entry = float(r["close"])
        stop = float(cur["low"] - 0.2 * cur["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue

        pos = {
            "entry_time": ts.isoformat(),
            "entry": entry,
            "stop": stop,
            "t1": entry + 1.5 * risk,
            "t2": entry + 2.2 * risk,
            "bars": 0,
            "t1_done": False,
            "partial": 0.0,
            "last_check": ts,
        }

    return trades


def main():
    key = os.getenv("SINO_API_KEY")
    sec = os.getenv("SINO_SECRET_KEY")
    if not key or not sec:
        raise SystemExit("請設定 SINO_API_KEY / SINO_SECRET_KEY")

    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    c = api.Contracts.Futures.MXF.MXFR1
    kb = api.kbars(c, start=os.getenv("BT_START", "2024-01-01"), end=os.getenv("BT_END", "2026-03-02"))
    api.logout()

    df = pd.DataFrame({**kb})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df.sort_values("ts").drop_duplicates(subset=["ts"]).set_index("ts")

    m1 = df[["open", "high", "low", "close", "volume"]].copy()
    h1 = add_indicators(resample_ohlc(m1, "60min"))
    d1 = resample_ohlc(m1, "1D")
    d1["ema20"] = d1["close"].ewm(span=20, adjust=False).mean()
    d1["ema20_prev"] = d1["ema20"].shift(1)

    t1 = backtest_v1(h1, d1)
    t2 = backtest_v2_intra(h1, d1, m1)

    out = {
        "dataset": {
            "rows_1m": int(len(m1)),
            "rows_60m": int(len(h1)),
            "rows_1d": int(len(d1)),
        },
        "v1_60m_close_entry": summarize(t1),
        "v2_60m_setup_1m_trigger": summarize(t2),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
