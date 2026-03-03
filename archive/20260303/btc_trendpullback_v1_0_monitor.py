#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path
import requests
import pandas as pd
import numpy as np

BASE = "https://api.binance.com"
SYMBOL = "BTCUSDT"
STATE = Path('/home/jasonpan_subscribe/.openclaw/workspace/data/binance/btc_trendpullback_v1_0_state.json')


def fetch_klines(interval='1m', limit=3000):
    r = requests.get(f"{BASE}/api/v3/klines", params={"symbol": SYMBOL, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tbv","tqv","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['ts'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    return df[['ts','open','high','low','close','volume']].dropna().set_index('ts').sort_index()


def resample(df, rule):
    x = pd.DataFrame()
    x['open'] = df['open'].resample(rule).first()
    x['high'] = df['high'].resample(rule).max()
    x['low'] = df['low'].resample(rule).min()
    x['close'] = df['close'].resample(rule).last()
    x['volume'] = df['volume'].resample(rule).sum()
    return x.dropna()


def indicators(df):
    o = df.copy()
    o['ema20'] = o['close'].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        o['high'] - o['low'],
        (o['high'] - o['close'].shift(1)).abs(),
        (o['low'] - o['close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o['atr14'] = tr.ewm(alpha=1/14, adjust=False).mean()
    o['vol_sma20'] = o['volume'].rolling(20).mean()
    return o


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding='utf-8'))
    return {'last_signal_key': None}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    m1 = fetch_klines('1m', 4000)
    h1 = indicators(resample(m1, '60min'))
    d1 = resample(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    if len(h1) < 25:
        return 0

    setup = h1.iloc[-2]
    prev = h1.iloc[-3]
    setup_time = h1.index[-2]
    next_time = h1.index[-1]

    day = setup_time.floor('D') - pd.Timedelta(days=1)
    if day not in d1.index:
        return 0
    d = d1.loc[day]

    trend = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
    near = abs(setup['low'] - setup['ema20']) <= 0.3*setup['atr14']
    bullish = (setup['close'] > setup['open']) and (setup['close'] > prev['high'])
    vol_ok = (setup['volume'] >= 0.9*setup['vol_sma20']) if not np.isnan(setup['vol_sma20']) else False

    if not (trend and near and bullish and vol_ok and setup['atr14'] > 0):
        return 0

    ew = m1[(m1.index > setup_time) & (m1.index <= next_time)].copy()
    if len(ew) < 25:
        return 0
    ew['ema20'] = ew['close'].ewm(span=20, adjust=False).mean()
    ew['hh5'] = ew['high'].rolling(5).max().shift(1)

    trig = None
    for ts, r in ew.iterrows():
        if np.isnan(r['ema20']) or np.isnan(r['hh5']):
            continue
        if r['close'] > r['hh5'] and r['close'] > r['ema20']:
            trig = (ts, float(r['close']))
            break
    if trig is None:
        return 0

    ts, entry = trig
    stop = float(setup['low'] - 0.2*setup['atr14'])
    risk = entry - stop
    if risk <= 0:
        return 0
    t1 = entry + 1.5*risk
    t2 = entry + 2.2*risk

    key = f"{setup_time.isoformat()}::{ts.isoformat()}"
    state = load_state()
    if state.get('last_signal_key') == key:
        return 0

    text = (
        f"TrendPullback_v1.0-H1-L-BTC 訊號\n"
        f"Setup: {setup_time.isoformat()}\n"
        f"Trigger: {ts.isoformat()}\n"
        f"進場: {entry:.2f}\n停損: {stop:.2f}\n停利1: {t1:.2f}\n停利2: {t2:.2f}"
    )
    subprocess.run(['openclaw','system','event','--text',text,'--mode','now'], check=False)
    state['last_signal_key'] = key
    save_state(state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
