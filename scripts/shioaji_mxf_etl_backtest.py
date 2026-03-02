#!/usr/bin/env python3
import os
import json
from dataclasses import dataclass

import pandas as pd
import numpy as np
import shioaji as sj

OUT_DIR = "data/shioaji"


def ensure_out():
    os.makedirs(OUT_DIR, exist_ok=True)


def load_1m(api, contract_code="MXFR1", start="2024-01-01", end="2026-03-02"):
    contract = getattr(api.Contracts.Futures.MXF, contract_code)
    kb = api.kbars(contract, start=start, end=end)
    df = pd.DataFrame({**kb})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume", "Amount": "amount"})
    df = df.sort_values("ts").drop_duplicates(subset=["ts"]).set_index("ts")
    return df


def resample_ohlc(df, rule):
    ohlc = pd.DataFrame()
    ohlc["open"] = df["open"].resample(rule).first()
    ohlc["high"] = df["high"].resample(rule).max()
    ohlc["low"] = df["low"].resample(rule).min()
    ohlc["close"] = df["close"].resample(rule).last()
    ohlc["volume"] = df["volume"].resample(rule).sum()
    ohlc = ohlc.dropna()
    return ohlc


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
    stop: float
    t1: float
    t2: float
    reason: str
    ret: float


def backtest_conservative(h1, d1):
    trades = []
    pos = None

    daily = d1.copy()
    daily["ema20"] = daily["close"].ewm(span=20, adjust=False).mean()
    daily["ema20_prev"] = daily["ema20"].shift(1)

    for i in range(30, len(h1)):
        row = h1.iloc[i]
        prev = h1.iloc[i-1]

        day = row.name.floor("D")
        if day not in daily.index:
            continue
        d = daily.loc[day]
        trend_long = (d["close"] > d["ema20"]) and (d["ema20"] > d["ema20_prev"])

        if pos is not None:
            pos["bars"] += 1

            if row["low"] <= pos["stop"]:
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], pos["stop"], pos["stop"], pos["t1"], pos["t2"], "stop", (pos["stop"]-pos["entry"])/pos["entry"]))
                pos = None
                continue

            if (not pos["t1_done"]) and (row["high"] >= pos["t1"]):
                pos["t1_done"] = True
                pos["partial"] = 0.5 * ((pos["t1"] - pos["entry"]) / pos["entry"])

            if row["high"] >= pos["t2"]:
                remain = 0.5 if pos["t1_done"] else 1.0
                total = pos["partial"] + remain * ((pos["t2"] - pos["entry"]) / pos["entry"])
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], pos["t2"], pos["stop"], pos["t1"], pos["t2"], "target", total))
                pos = None
                continue

            if (pos["bars"] >= 6) or (row["close"] < row["ema20"]):
                remain = 0.5 if pos["t1_done"] else 1.0
                total = pos["partial"] + remain * ((row["close"] - pos["entry"]) / pos["entry"])
                trades.append(Trade(pos["entry_time"], row.name.isoformat(), pos["entry"], row["close"], pos["stop"], pos["t1"], pos["t2"], "time_or_ema", total))
                pos = None
                continue

        if pos is None and trend_long:
            near_ema = abs(row["low"] - row["ema20"]) <= 0.3 * row["atr14"]
            bullish = (row["close"] > row["open"]) and (row["close"] > prev["high"])
            vol_ok = row["volume"] >= 0.9 * row["vol_sma20"] if not np.isnan(row["vol_sma20"]) else False
            if near_ema and bullish and vol_ok and row["atr14"] > 0:
                stop = row["low"] - 0.2 * row["atr14"]
                risk = row["close"] - stop
                if risk <= 0:
                    continue
                pos = {
                    "entry_time": row.name.isoformat(),
                    "entry": float(row["close"]),
                    "stop": float(stop),
                    "t1": float(row["close"] + 1.5*risk),
                    "t2": float(row["close"] + 2.2*risk),
                    "bars": 0,
                    "t1_done": False,
                    "partial": 0.0,
                }

    return trades


def summarize(trades):
    if not trades:
        return {"trades": 0}
    rets = np.array([t.ret for t in trades])
    wins = (rets > 0).sum()
    gross_p = rets[rets > 0].sum()
    gross_l = -rets[rets < 0].sum()
    pf = float(gross_p / gross_l) if gross_l > 0 else None

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
        "profit_factor": pf,
        "mdd": float(mdd),
        "equity_multiple": float(eq),
    }


def main():
    key = os.getenv("SINO_API_KEY")
    sec = os.getenv("SINO_SECRET_KEY")
    if not key or not sec:
        raise SystemExit("請設定 SINO_API_KEY / SINO_SECRET_KEY")

    api = sj.Shioaji(simulation=True)
    accts = api.login(api_key=key, secret_key=sec)

    ensure_out()

    df1m = load_1m(api, contract_code=os.getenv("SINO_CONTRACT", "MXFR1"), start=os.getenv("BT_START", "2024-01-01"), end=os.getenv("BT_END", "2026-03-02"))
    h1 = add_indicators(resample_ohlc(df1m, "60min"))
    d1 = resample_ohlc(df1m, "1D")

    h1.reset_index().to_csv(f"{OUT_DIR}/MXF_60m.csv", index=False)
    d1.reset_index().to_csv(f"{OUT_DIR}/MXF_1d.csv", index=False)

    trades = backtest_conservative(h1, d1)
    summary = summarize(trades)

    tdf = pd.DataFrame([t.__dict__ for t in trades])
    if not tdf.empty:
        tdf.to_csv(f"{OUT_DIR}/MXF_backtest_trades.csv", index=False)

    out = {
        "mode": "simulation",
        "accounts": len(accts) if accts else 0,
        "rows_1m": int(len(df1m)),
        "rows_60m": int(len(h1)),
        "rows_1d": int(len(d1)),
        "summary": summary,
    }
    with open(f"{OUT_DIR}/MXF_backtest_summary.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    api.logout()


if __name__ == "__main__":
    main()
