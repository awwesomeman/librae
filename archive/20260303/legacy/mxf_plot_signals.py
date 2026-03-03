#!/usr/bin/env python3
import os
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
import shioaji as sj

OUT_DIR = "data/shioaji"
os.makedirs(OUT_DIR, exist_ok=True)


def resample_ohlc(df, rule):
    ohlc = pd.DataFrame()
    ohlc["Open"] = df["open"].resample(rule).first()
    ohlc["High"] = df["high"].resample(rule).max()
    ohlc["Low"] = df["low"].resample(rule).min()
    ohlc["Close"] = df["close"].resample(rule).last()
    ohlc["Volume"] = df["volume"].resample(rule).sum()
    return ohlc.dropna()


def add_indicators(df):
    out = df.copy()
    out["ema20"] = out["Close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        out["High"] - out["Low"],
        (out["High"] - out["Close"].shift(1)).abs(),
        (out["Low"] - out["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    out["vol_sma20"] = out["Volume"].rolling(20).mean()
    return out


def long_setup(h1_row, h1_prev, d1_row):
    trend_long = (d1_row["Close"] > d1_row["ema20"]) and (d1_row["ema20"] > d1_row["ema20_prev"])
    near_ema = abs(h1_row["Low"] - h1_row["ema20"]) <= 0.3 * h1_row["atr14"]
    bullish = (h1_row["Close"] > h1_row["Open"]) and (h1_row["Close"] > h1_prev["High"])
    vol_ok = h1_row["Volume"] >= 0.9 * h1_row["vol_sma20"] if not np.isnan(h1_row["vol_sma20"]) else False
    return trend_long and near_ema and bullish and vol_ok


def backtest_v2(h1, d1, m1):
    trades = []
    pos = None

    for i in range(30, len(h1) - 1):
        cur = h1.iloc[i]
        prev = h1.iloc[i - 1]
        cur_time = h1.index[i]
        next_time = h1.index[i + 1]

        day = cur_time.floor("D") - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]

        if pos is not None:
            window = m1[(m1.index > pos["last_check"]) & (m1.index <= cur_time)]
            for ts, r in window.iterrows():
                if r["Low"] <= pos["stop"]:
                    trades.append({**pos, "exit_time": ts, "exit": pos["stop"], "reason": "stop"})
                    pos = None
                    break
                if (not pos["t1_done"]) and (r["High"] >= pos["t1"]):
                    pos["t1_done"] = True
                if r["High"] >= pos["t2"]:
                    trades.append({**pos, "exit_time": ts, "exit": pos["t2"], "reason": "target"})
                    pos = None
                    break
            if pos is not None:
                pos["bars"] += 1
                if (pos["bars"] >= 6) or (cur["Close"] < cur["ema20"]):
                    trades.append({**pos, "exit_time": cur_time, "exit": cur["Close"], "reason": "time_or_ema"})
                    pos = None
                else:
                    pos["last_check"] = cur_time

        if pos is not None:
            continue

        if not long_setup(cur, prev, d) or cur["atr14"] <= 0:
            continue

        entry_window = m1[(m1.index > cur_time) & (m1.index <= next_time)].copy()
        if entry_window.empty:
            continue

        entry_window["ema20"] = entry_window["Close"].ewm(span=20, adjust=False).mean()
        entry_window["hh5"] = entry_window["High"].rolling(5).max().shift(1)

        trigger = None
        for ts, r in entry_window.iterrows():
            if np.isnan(r["hh5"]) or np.isnan(r["ema20"]):
                continue
            if (r["Close"] > r["hh5"]) and (r["Close"] > r["ema20"]):
                trigger = (ts, r)
                break

        if trigger is None:
            continue

        ts, r = trigger
        entry = float(r["Close"])
        stop = float(cur["Low"] - 0.2 * cur["atr14"])
        risk = entry - stop
        if risk <= 0:
            continue

        pos = {
            "entry_time": ts,
            "entry": entry,
            "stop": stop,
            "t1": entry + 1.5 * risk,
            "t2": entry + 2.2 * risk,
            "bars": 0,
            "t1_done": False,
            "last_check": ts,
        }

    return pd.DataFrame(trades)


def main():
    key = os.getenv("SINO_API_KEY")
    sec = os.getenv("SINO_SECRET_KEY")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    kb = api.kbars(api.Contracts.Futures.MXF.MXFR1, start="2024-01-01", end="2026-03-02")
    api.logout()

    df = pd.DataFrame({**kb})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"})
    df = df.sort_values("ts").drop_duplicates(subset=["ts"]).set_index("ts")

    m1 = resample_ohlc(df, "1min")
    h1 = add_indicators(resample_ohlc(df, "60min"))
    d1 = resample_ohlc(df, "1D")
    d1["ema20"] = d1["Close"].ewm(span=20, adjust=False).mean()
    d1["ema20_prev"] = d1["ema20"].shift(1)

    trades = backtest_v2(h1, d1, m1)
    trades.to_csv(f"{OUT_DIR}/MXF_v2_trades.csv", index=False)

    # 畫最近 60 天 60m K 線 + 交易標記
    cutoff = h1.index.max() - pd.Timedelta(days=60)
    plot_df = h1[h1.index >= cutoff].copy()
    trades_recent = trades[pd.to_datetime(trades["entry_time"]) >= cutoff].copy()

    entry_mark = pd.Series(np.nan, index=plot_df.index)
    exit_mark = pd.Series(np.nan, index=plot_df.index)

    for _, t in trades_recent.iterrows():
        et = pd.to_datetime(t["entry_time"]).floor("60min")
        xt = pd.to_datetime(t["exit_time"]).floor("60min")
        if et in entry_mark.index:
            entry_mark.loc[et] = t["entry"]
        if xt in exit_mark.index:
            exit_mark.loc[xt] = t["exit"]

    ap = [
        mpf.make_addplot(plot_df["ema20"], color="dodgerblue", width=1.0),
        mpf.make_addplot(entry_mark, type="scatter", marker="^", markersize=70, color="lime"),
        mpf.make_addplot(exit_mark, type="scatter", marker="v", markersize=70, color="red"),
    ]

    fig, axlist = mpf.plot(
        plot_df,
        type="candle",
        volume=True,
        addplot=ap,
        style="yahoo",
        figratio=(16, 9),
        figscale=1.2,
        returnfig=True,
        title="MXF Strategy v2 (60m setup + 1m trigger)\nGreen=Entry, Red=Exit, Blue=EMA20",
    )

    # 額外畫出每筆交易的停損/停利區間線（只畫最近60天）
    price_ax = axlist[0]
    for _, t in trades_recent.iterrows():
        et = pd.to_datetime(t["entry_time"]).floor("60min")
        xt = pd.to_datetime(t["exit_time"]).floor("60min")
        if et in plot_df.index and xt in plot_df.index:
            price_ax.hlines(t["stop"], xmin=et, xmax=xt, colors="orange", linestyles="dotted", linewidth=0.8)
            price_ax.hlines(t["t1"], xmin=et, xmax=xt, colors="gray", linestyles="dashed", linewidth=0.8)
            price_ax.hlines(t["t2"], xmin=et, xmax=xt, colors="purple", linestyles="dashdot", linewidth=0.8)

    out_png = f"{OUT_DIR}/MXF_v2_signals_60d.png"
    fig.savefig(out_png, dpi=150, bbox_inches="tight")

    summary = {
        "trades_total": int(len(trades)),
        "plotted_trades": int(len(trades_recent)),
        "chart": out_png,
        "trades_csv": f"{OUT_DIR}/MXF_v2_trades.csv",
    }
    with open(f"{OUT_DIR}/MXF_v2_plot_meta.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
