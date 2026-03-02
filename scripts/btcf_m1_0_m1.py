#!/usr/bin/env python3
import json
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

BASE = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
COST_BPS_ROUNDTRIP = 10  # futures conservative


def fetch_klines(interval: str, start_ms: int, end_ms: int):
    import time
    out = []
    cur = start_ms
    while cur < end_ms:
        params = {"symbol": SYMBOL, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": 1500}
        for _ in range(5):
            r = requests.get(f"{BASE}/fapi/v1/klines", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.2)
                continue
            r.raise_for_status()
            rows = r.json()
            break
        else:
            raise RuntimeError("Rate limit retries exceeded")
        if not rows:
            break
        out.extend(rows)
        cur = int(rows[-1][0]) + 1
        if len(rows) < 1500:
            break
        time.sleep(0.12)
    return out


def to_df(rows):
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tbv","tqv","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["ts","open","high","low","close","volume"]].dropna().set_index("ts").sort_index()


def resample(df, rule):
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()


def add_feat(h1):
    o = h1.copy()
    o["ema20"] = o["close"].ewm(span=20, adjust=False).mean()
    o["ema60"] = o["close"].ewm(span=60, adjust=False).mean()
    tr = pd.concat([
        o["high"] - o["low"],
        (o["high"] - o["close"].shift(1)).abs(),
        (o["low"] - o["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    o["vol_sma20"] = o["volume"].rolling(20).mean()
    o["ret20"] = o["close"].pct_change(20)
    o["ret60"] = o["close"].pct_change(60)
    o["atrp"] = o["atr14"] / o["close"]
    return o


def score(r):
    trend = 100 if (r.close > r.ema20 > r.ema60) else (60 if r.close > r.ema20 else 20)
    mom_raw = 0 if np.isnan(r.ret20) or np.isnan(r.ret60) else (0.6*r.ret20 + 0.4*r.ret60)
    mom = np.clip(50 + mom_raw*1200, 0, 100)
    volq = 40 if np.isnan(r.vol_sma20) or r.vol_sma20 <= 0 else np.clip((r.volume/r.vol_sma20)*70, 0, 100)
    vol_pen = 50 if np.isnan(r.atrp) else np.clip(100 - r.atrp*7000, 0, 100)
    return 0.35*trend + 0.30*mom + 0.20*volq + 0.15*vol_pen


def run_bt(m1,h1,d1,start,end,th=70):
    h = h1[(h1.index>=start)&(h1.index<=end)]
    rets=[]; pos=None
    for i in range(70,len(h)-1):
        cur=h.iloc[i]; prev=h.iloc[i-1]; t=h.index[i]; nt=h.index[i+1]
        if pos is not None:
            w=m1[(m1.index>pos['last'])&(m1.index<=t)]
            for _,r in w.iterrows():
                if r.low<=pos['stop']:
                    rets.append((pos['stop']-pos['entry'])/pos['entry']-COST_BPS_ROUNDTRIP/10000); pos=None; break
                if (not pos['t1d']) and r.high>=pos['t1']:
                    pos['t1d']=True; pos['part']=0.5*((pos['t1']-pos['entry'])/pos['entry'])
                if r.high>=pos['t2']:
                    rem=0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part']+rem*((pos['t2']-pos['entry'])/pos['entry'])-COST_BPS_ROUNDTRIP/10000); pos=None; break
            if pos is not None:
                pos['bars']+=1
                if pos['bars']>=6 or cur.close<cur.ema20:
                    rets.append((cur.close-pos['entry'])/pos['entry']-COST_BPS_ROUNDTRIP/10000); pos=None
                else:
                    pos['last']=t
        if pos is not None:
            continue
        day=t.floor('D')-pd.Timedelta(days=1)
        if day not in d1.index: continue
        d=d1.loc[day]
        trend_gate=(d.close>d.ema20) and (d.ema20>d.ema20_prev)
        setup=trend_gate and (abs(cur.low-cur.ema20)<=0.3*cur.atr14) and (cur.close>cur.open) and (cur.close>prev.high)
        if not setup or score(cur)<th: continue
        ew=m1[(m1.index>t)&(m1.index<=nt)].copy()
        if len(ew)<25: continue
        ew['ema10']=ew['close'].ewm(span=10,adjust=False).mean(); ew['hh3']=ew['high'].rolling(3).max().shift(1)
        trig=None
        for ts,r in ew.iterrows():
            if np.isnan(r.ema10) or np.isnan(r.hh3): continue
            if r.close>r.hh3 and r.close>r.ema10:
                trig=(ts,float(r.close)); break
        if trig is None: continue
        ts,entry=trig
        stop=float(cur.low-0.2*cur.atr14); risk=entry-stop
        if risk<=0: continue
        pos={'entry':entry,'stop':stop,'t1':entry+1.5*risk,'t2':entry+2.2*risk,'bars':0,'t1d':False,'part':0.0,'last':ts}
    if not rets: return {'trades':0}
    arr=np.array(rets); wins=(arr>0).sum(); gp=arr[arr>0].sum(); gl=-arr[arr<0].sum(); pf=gp/gl if gl>0 else None
    eq=np.cumprod(1+arr); peak=np.maximum.accumulate(eq); mdd=float(np.max((peak-eq)/peak))
    years=(pd.Timestamp(end)-pd.Timestamp(start)).days/365.25
    ann=float(eq[-1]**(1/years)-1) if years>0 else None
    tpy=len(arr)/years if years>0 else 0
    vol=float(arr.std(ddof=1)*np.sqrt(tpy)) if len(arr)>1 else None
    sharpe=float((arr.mean()/arr.std(ddof=1))*np.sqrt(tpy)) if len(arr)>1 and arr.std(ddof=1)>0 else None
    return {'trades':int(len(arr)),'win_rate':float(wins/len(arr)),'avg_ret':float(arr.mean()),'pf':float(pf) if pf else None,'ann_return':ann,'ann_sharpe':sharpe,'ann_vol':vol,'mdd':mdd,'equity':float(eq[-1])}


def main():
    start = int(datetime(2025,1,1,tzinfo=timezone.utc).timestamp()*1000)
    end = int(datetime.now(tz=timezone.utc).timestamp()*1000)
    m1 = to_df(fetch_klines('1m', start, end))
    h1 = add_feat(resample(m1,'60min'))
    d1 = resample(m1,'1D')
    d1['ema20']=d1['close'].ewm(span=20,adjust=False).mean(); d1['ema20_prev']=d1['ema20'].shift(1)
    train = run_bt(m1,h1,d1,'2025-01-01','2025-09-30',70)
    oos = run_bt(m1,h1,d1,'2025-10-01',pd.Timestamp.utcnow().strftime('%Y-%m-%d'),70)
    print(json.dumps({'strategy':'MultiFactorScore_v1.0-H1-LS-BTCF','data':'Binance USDS-M Futures','train':train,'oos':oos},ensure_ascii=False,indent=2))

if __name__=='__main__':
    main()
