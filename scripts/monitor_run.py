#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import subprocess
from pathlib import Path
import shlex
import pandas as pd
from monitor_core import (
    fetch_binance_klines, resample_ohlcv, add_indicators,
    load_state, save_state, trendpullback_setup_ok, trendpullback_trigger
)


def run_btc_trendpullback(stage: str, cfg: dict):
    state = load_state(cfg['state_file'])
    m1 = fetch_binance_klines(cfg['symbol'], '1m', cfg.get('limit', 4000))
    h1 = add_indicators(resample_ohlcv(m1, '60min'))
    d1 = resample_ohlcv(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    if len(h1) < 25:
        return

    setup = h1.iloc[-2]
    prev = h1.iloc[-3]
    setup_time = h1.index[-2]
    next_time = h1.index[-1]

    if stage == 'setup':
        ok = trendpullback_setup_ok(setup, prev, d1, setup_time, cfg['params']['pull'], cfg['params']['vol_ratio'])
        state['setup_ready'] = bool(ok)
        state['setup_time'] = setup_time.isoformat() if ok else None
        save_state(cfg['state_file'], state)
        return

    # trigger stage
    if not state.get('setup_ready'):
        return
    if state.get('setup_time') != setup_time.isoformat():
        return

    ew = m1[(m1.index > setup_time) & (m1.index <= next_time)]
    trg = trendpullback_trigger(ew, cfg['params']['breakout_n'], cfg['params']['trigger_ema'])
    if trg is None:
        return
    ts, entry = trg
    stop = float(setup['low'] - 0.2 * setup['atr14'])
    risk = entry - stop
    if risk <= 0:
        return

    key = f"{setup_time.isoformat()}::{ts.isoformat()}"
    if state.get('last_signal_key') == key:
        return

    t1 = entry + 1.5 * risk
    t2 = entry + 2.2 * risk
    text = (
        f"{cfg['strategy']} 訊號\n"
        f"Setup: {setup_time.isoformat()}\n"
        f"Trigger: {ts.isoformat()}\n"
        f"進場: {entry:.2f}\n停損: {stop:.2f}\n停利1: {t1:.2f}\n停利2: {t2:.2f}"
    )
    subprocess.run(['openclaw', 'system', 'event', '--text', text, '--mode', 'now'], check=False)
    state['last_signal_key'] = key
    save_state(cfg['state_file'], state)


def run_shioaji_legacy_adapter(stage: str, cfg: dict):
    script = cfg['setup_script'] if stage == 'setup' else cfg['trigger_script']
    env_file = cfg['env_file']
    cmd = f"source {shlex.quote(env_file)} && /home/jasonpan_subscribe/.openclaw/workspace/.venv/bin/python {shlex.quote(script)}"
    subprocess.run(['bash', '-lc', cmd], check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True)
    ap.add_argument('--stage', required=True, choices=['setup', 'trigger'])
    args = ap.parse_args()

    cfg = json.loads(Path(args.profile).read_text(encoding='utf-8'))
    if cfg['kind'] == 'binance_trendpullback':
        run_btc_trendpullback(args.stage, cfg)
    elif cfg['kind'] == 'shioaji_mxf_legacy_adapter':
        run_shioaji_legacy_adapter(args.stage, cfg)
    else:
        raise SystemExit(f"unsupported kind: {cfg['kind']}")


if __name__ == '__main__':
    main()
