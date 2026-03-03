#!/usr/bin/env python3
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd
import numpy as np
import requests
from datetime import datetime, timezone
import os
import shioaji as sj

BASE = "https://api.binance.com"


def fetch_binance_klines(symbol: str, interval: str = '1m', limit: int = 4000) -> pd.DataFrame:
    r = requests.get(f"{BASE}/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=20)
    r.raise_for_status()
    rows = r.json()
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tbv","tqv","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['ts'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    return df[['ts','open','high','low','close','volume']].dropna().set_index('ts').sort_index()


def fetch_shioaji_mxf_kbars(start: str, end: str, simulation: bool = True) -> pd.DataFrame:
    api_key = os.getenv('SINO_API_KEY')
    secret_key = os.getenv('SINO_SECRET_KEY')
    if not api_key or not secret_key:
        return pd.DataFrame()

    api = sj.Shioaji(simulation=simulation)
    api.login(api_key=api_key, secret_key=secret_key)
    kb = api.kbars(api.Contracts.Futures.MXF.MXFR1, start=start, end=end)
    api.logout()

    df = pd.DataFrame({**kb})
    if df.empty:
        return df
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.rename(columns={
        'Open': 'open', 'High': 'high', 'Low': 'low',
        'Close': 'close', 'Volume': 'volume'
    }).set_index('ts').sort_index()
    return df[['open', 'high', 'low', 'close', 'volume']]


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    x = pd.DataFrame()
    x['open'] = df['open'].resample(rule).first()
    x['high'] = df['high'].resample(rule).max()
    x['low'] = df['low'].resample(rule).min()
    x['close'] = df['close'].resample(rule).last()
    x['volume'] = df['volume'].resample(rule).sum()
    return x.dropna()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
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


def load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding='utf-8'))
    return {}


def save_state(path: str, state: dict):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def trendpullback_setup_ok(setup: pd.Series, prev: pd.Series, d1: pd.DataFrame, setup_time: pd.Timestamp, pull=0.3, vol_ratio=0.9) -> bool:
    day = setup_time.floor('D') - pd.Timedelta(days=1)
    if day not in d1.index:
        return False
    d = d1.loc[day]
    trend = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
    near = abs(setup['low'] - setup['ema20']) <= pull * setup['atr14']
    bullish = (setup['close'] > setup['open']) and (setup['close'] > prev['high'])
    vol_ok = (setup['volume'] >= vol_ratio * setup['vol_sma20']) if not np.isnan(setup['vol_sma20']) else False
    return bool(trend and near and bullish and vol_ok and setup['atr14'] > 0)


def trendpullback_trigger(m1_window: pd.DataFrame, breakout_n=5, trigger_ema=20):
    ew = m1_window.copy()
    if len(ew) < max(breakout_n + 3, trigger_ema + 3):
        return None
    ew['ema'] = ew['close'].ewm(span=trigger_ema, adjust=False).mean()
    ew['hh'] = ew['high'].rolling(breakout_n).max().shift(1)
    for ts, r in ew.iterrows():
        if np.isnan(r['ema']) or np.isnan(r['hh']):
            continue
        if r['close'] > r['hh'] and r['close'] > r['ema']:
            return ts, float(r['close'])
    return None


def append_signal_log(
    log_file: str,
    strategy: str,
    stage: str,
    status: str,
    signal_key: str | None = None,
    setup_time: str | None = None,
    trigger_time: str | None = None,
    entry: float | None = None,
    stop: float | None = None,
    take_profit_1: float | None = None,
    take_profit_2: float | None = None,
    message: str | None = None,
):
    row = {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'strategy': strategy,
        'stage': stage,
        'status': status,
        'signal_key': signal_key,
        'setup_time': setup_time,
        'trigger_time': trigger_time,
        'entry': entry,
        'stop': stop,
        'take_profit_1': take_profit_1,
        'take_profit_2': take_profit_2,
        'message': message,
    }
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
        path.replace(path.parent / (path.name + '.1'))
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
