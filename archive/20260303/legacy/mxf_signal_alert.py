#!/usr/bin/env python3
import os
import json
import subprocess
from pathlib import Path

import pandas as pd
import numpy as np
import shioaji as sj

STATE_PATH = Path('/home/jasonpan_subscribe/.openclaw/workspace/data/shioaji/mxf_signal_state.json')


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


def resample(df, rule):
    x = pd.DataFrame()
    x['open'] = df['open'].resample(rule).first()
    x['high'] = df['high'].resample(rule).max()
    x['low'] = df['low'].resample(rule).min()
    x['close'] = df['close'].resample(rule).last()
    x['volume'] = df['volume'].resample(rule).sum()
    return x.dropna()


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding='utf-8'))
    return {'last_signal_key': None}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def long_setup(h1_row, h1_prev, d1_row):
    trend_long = (d1_row['close'] > d1_row['ema20']) and (d1_row['ema20'] > d1_row['ema20_prev'])
    near_ema = abs(h1_row['low'] - h1_row['ema20']) <= 0.3 * h1_row['atr14']
    bullish = (h1_row['close'] > h1_row['open']) and (h1_row['close'] > h1_prev['high'])
    vol_ok = h1_row['volume'] >= 0.9 * h1_row['vol_sma20'] if not np.isnan(h1_row['vol_sma20']) else False
    return trend_long and near_ema and bullish and vol_ok


def main():
    key = os.getenv('SINO_API_KEY')
    sec = os.getenv('SINO_SECRET_KEY')
    if not key or not sec:
        print('Missing SINO_API_KEY/SINO_SECRET_KEY')
        return 1

    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)

    c = api.Contracts.Futures.MXF.MXFR1
    kb = api.kbars(c, start=os.getenv('SIG_START', '2026-01-01'), end=os.getenv('SIG_END', '2026-12-31'))
    df = pd.DataFrame({**kb})
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}).set_index('ts').sort_index()

    m1 = df[['open', 'high', 'low', 'close', 'volume']].copy()
    h1 = indicators(resample(m1, '60min'))
    d1 = resample(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    if len(h1) < 25:
        api.logout()
        return 0

    # 使用「上一根已收60mK」當 setup，並在當前60m內用1m找突破進場
    setup_bar = h1.iloc[-2]
    prev_bar = h1.iloc[-3]
    setup_time = h1.index[-2]
    next_time = h1.index[-1]

    day = setup_time.floor('D') - pd.Timedelta(days=1)
    if day not in d1.index:
        api.logout()
        return 0
    d = d1.loc[day]

    setup_ok = long_setup(setup_bar, prev_bar, d)
    if not setup_ok:
        api.logout()
        return 0

    window = m1[(m1.index > setup_time) & (m1.index <= next_time)].copy()
    if len(window) < 25:
        api.logout()
        return 0

    window['ema20'] = window['close'].ewm(span=20, adjust=False).mean()
    window['hh5'] = window['high'].rolling(5).max().shift(1)

    trigger = None
    for ts, r in window.iterrows():
        if np.isnan(r['ema20']) or np.isnan(r['hh5']):
            continue
        if (r['close'] > r['hh5']) and (r['close'] > r['ema20']):
            trigger = (ts, r)
            break

    if trigger is None:
        api.logout()
        return 0

    ts, r = trigger
    entry = float(r['close'])
    stop = float(setup_bar['low'] - 0.2 * setup_bar['atr14'])
    risk = entry - stop
    if risk <= 0:
        api.logout()
        return 0

    t1 = entry + 1.5 * risk
    t2 = entry + 2.2 * risk
    signal_key = f"{setup_time.isoformat()}::{ts.isoformat()}"

    state = load_state()
    if state.get('last_signal_key') == signal_key:
        api.logout()
        return 0

    text = (
        f"MXF 訊號觸發（優化版）\n"
        f"Setup(60m): {setup_time.isoformat()}\n"
        f"Trigger(1m): {ts.isoformat()}\n"
        f"方向: LONG_ENTRY\n"
        f"進場: {entry:.1f}\n"
        f"停損: {stop:.1f}\n"
        f"停利1: {t1:.1f}\n"
        f"停利2: {t2:.1f}\n"
        f"邏輯: 日線趨勢多 + 60m setup + 1m突破觸發"
    )
    subprocess.run(['openclaw', 'system', 'event', '--text', text, '--mode', 'now'], check=False)

    state['last_signal_key'] = signal_key
    save_state(state)

    api.logout()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
