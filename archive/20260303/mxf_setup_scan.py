#!/usr/bin/env python3
import os, json
from pathlib import Path
import pandas as pd
import numpy as np
import shioaji as sj

STATE = Path('/home/jasonpan_subscribe/.openclaw/workspace/data/shioaji/mxf_monitor_state.json')


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
    return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    key = os.getenv('SINO_API_KEY'); sec = os.getenv('SINO_SECRET_KEY')
    if not key or not sec:
        return 1

    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    kb = api.kbars(api.Contracts.Futures.MXF.MXFR1, start='2026-01-01', end='2026-12-31')
    api.logout()

    df = pd.DataFrame({**kb})
    if df.empty:
        return 0
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.rename(columns={'Open':'open','High':'high','Low':'low','Close':'close','Volume':'volume'}).set_index('ts').sort_index()

    m1 = df[['open','high','low','close','volume']]
    h1 = indicators(resample(m1, '60min'))
    d1 = resample(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    if len(h1) < 3:
        return 0

    setup_bar = h1.iloc[-2]
    prev_bar = h1.iloc[-3]
    setup_time = h1.index[-2]
    day = setup_time.floor('D') - pd.Timedelta(days=1)
    if day not in d1.index:
        return 0
    d = d1.loc[day]

    trend = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
    near = abs(setup_bar['low'] - setup_bar['ema20']) <= 0.3 * setup_bar['atr14']
    bull = (setup_bar['close'] > setup_bar['open']) and (setup_bar['close'] > prev_bar['high'])
    vol = setup_bar['volume'] >= 0.9 * setup_bar['vol_sma20'] if not np.isnan(setup_bar['vol_sma20']) else False
    setup_ok = bool(trend and near and bull and vol)

    state = load_state()
    state['last_scan_at'] = pd.Timestamp.utcnow().isoformat()
    if setup_ok:
        state['active'] = True
        state['setup_time'] = setup_time.isoformat()
        state['expires_at'] = (setup_time + pd.Timedelta(hours=1)).isoformat()
        state['setup_low'] = float(setup_bar['low'])
        state['setup_atr14'] = float(setup_bar['atr14'])
        state['triggered'] = False
    else:
        # only deactivate if past previous window
        exp = pd.to_datetime(state.get('expires_at')) if state.get('expires_at') else None
        now = pd.Timestamp.utcnow()
        if exp is not None and now > exp:
            state['active'] = False
        elif exp is None:
            state['active'] = False
    save_state(state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
