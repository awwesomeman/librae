#!/usr/bin/env python3
import os, json, subprocess
from pathlib import Path
import pandas as pd
import numpy as np
import shioaji as sj

STATE = Path('/home/jasonpan_subscribe/.openclaw/workspace/data/shioaji/mxf_monitor_state.json')


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding='utf-8'))
    return {}


def save_state(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    s = load_state()
    if not s.get('active') or s.get('triggered'):
        return 0

    exp = pd.to_datetime(s.get('expires_at')) if s.get('expires_at') else None
    now = pd.Timestamp.utcnow()
    if exp is None or now > exp:
        s['active'] = False
        save_state(s)
        return 0

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

    setup_time = pd.to_datetime(s['setup_time'])
    w = df[(df.index > setup_time) & (df.index <= exp)].copy()
    if len(w) < 25:
        return 0

    w['ema20'] = w['close'].ewm(span=20, adjust=False).mean()
    w['hh5'] = w['high'].rolling(5).max().shift(1)

    trigger = None
    for ts, r in w.iterrows():
        if np.isnan(r['ema20']) or np.isnan(r['hh5']):
            continue
        if (r['close'] > r['hh5']) and (r['close'] > r['ema20']):
            trigger = (ts, r)
            break

    if trigger is None:
        return 0

    ts, r = trigger
    entry = float(r['close'])
    stop = float(s['setup_low'] - 0.2 * s['setup_atr14'])
    risk = entry - stop
    if risk <= 0:
        return 0
    t1 = entry + 1.5*risk
    t2 = entry + 2.2*risk

    text = (
      f"MXF 訊號觸發\n"
      f"Setup: {s['setup_time']}\n"
      f"Trigger: {ts.isoformat()}\n"
      f"進場: {entry:.1f}\n"
      f"停損: {stop:.1f}\n"
      f"停利1: {t1:.1f}\n"
      f"停利2: {t2:.1f}\n"
      f"邏輯: 60m setup 成立後，1m 突破觸發"
    )
    subprocess.run(['openclaw','system','event','--text',text,'--mode','now'], check=False)

    s['triggered'] = True
    s['trigger_time'] = ts.isoformat()
    save_state(s)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
