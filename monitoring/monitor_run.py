#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
import subprocess
from pathlib import Path
import pandas as pd
try:
    from monitoring.monitor_core import (
        fetch_binance_klines,
        fetch_shioaji_mxf_kbars,
        resample_ohlcv,
        add_indicators,
        load_state,
        save_state,
        trendpullback_setup_ok,
        trendpullback_trigger,
        append_signal_log,
    )
    from monitoring.utils_dedupe import build_signal_key, is_duplicate
except ImportError:
    from monitor_core import (
        fetch_binance_klines,
        fetch_shioaji_mxf_kbars,
        resample_ohlcv,
        add_indicators,
        load_state,
        save_state,
        trendpullback_setup_ok,
        trendpullback_trigger,
        append_signal_log,
    )
    from utils_dedupe import build_signal_key, is_duplicate


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
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='setup',
            status='setup_ready' if ok else 'setup_not_ready',
            setup_time=setup_time.isoformat(),
            message='setup scan completed',
        )
        return

    # trigger stage
    if not state.get('setup_ready'):
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='skip',
            setup_time=setup_time.isoformat(),
            message='setup not ready',
        )
        return
    if state.get('setup_time') != setup_time.isoformat():
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='skip',
            setup_time=setup_time.isoformat(),
            message='setup time mismatch',
        )
        return

    ew = m1[(m1.index > setup_time) & (m1.index <= next_time)]
    trg = trendpullback_trigger(ew, cfg['params']['breakout_n'], cfg['params']['trigger_ema'])
    if trg is None:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='no_trigger',
            setup_time=setup_time.isoformat(),
            message='no trigger in current window',
        )
        return
    ts, entry = trg
    stop = float(setup['low'] - 0.2 * setup['atr14'])
    risk = entry - stop
    if risk <= 0:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='invalid',
            setup_time=setup_time.isoformat(),
            trigger_time=ts.isoformat(),
            entry=entry,
            stop=stop,
            message='risk <= 0',
        )
        return

    t1 = entry + 1.5 * risk
    t2 = entry + 2.2 * risk
    key = build_signal_key(setup_time.isoformat(), ts.isoformat())
    if is_duplicate(state.get('last_signal_key'), key):
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='duplicate',
            signal_key=key,
            setup_time=setup_time.isoformat(),
            trigger_time=ts.isoformat(),
            entry=entry,
            stop=stop,
            take_profit_1=t1,
            take_profit_2=t2,
            message='duplicate signal ignored',
        )
        return
    text = (
        f"{cfg['strategy']} 訊號\n"
        f"Setup: {setup_time.isoformat()}\n"
        f"Trigger: {ts.isoformat()}\n"
        f"進場: {entry:.2f}\n停損: {stop:.2f}\n停利1: {t1:.2f}\n停利2: {t2:.2f}"
    )
    subprocess.run(['openclaw', 'system', 'event', '--text', text, '--mode', 'now'], check=False)
    append_signal_log(
        log_file=cfg['log_file'],
        strategy=cfg['strategy'],
        stage='trigger',
        status='signal_emitted',
        signal_key=key,
        setup_time=setup_time.isoformat(),
        trigger_time=ts.isoformat(),
        entry=entry,
        stop=stop,
        take_profit_1=t1,
        take_profit_2=t2,
        message='signal sent to event bus',
    )
    state['last_signal_key'] = key
    save_state(cfg['state_file'], state)


def run_tsi_trendpullback(stage: str, cfg: dict):
    state = load_state(cfg['state_file'])
    m1 = fetch_shioaji_mxf_kbars(cfg['data_start'], cfg['data_end'])
    if m1.empty:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage=stage,
            status='skip',
            message='no kbars data',
        )
        return

    h1 = add_indicators(resample_ohlcv(m1, '60min'))
    d1 = resample_ohlcv(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    if len(h1) < 25:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage=stage,
            status='skip',
            message='insufficient h1 bars',
        )
        return

    setup = h1.iloc[-2]
    prev = h1.iloc[-3]
    setup_time = h1.index[-2]
    next_time = h1.index[-1]

    if stage == 'setup':
        ok = trendpullback_setup_ok(setup, prev, d1, setup_time, cfg['params']['pull'], cfg['params']['vol_ratio'])
        state['setup_ready'] = bool(ok)
        state['setup_time'] = setup_time.isoformat() if ok else None
        state['setup_low'] = float(setup['low']) if ok else None
        state['setup_atr14'] = float(setup['atr14']) if ok else None
        save_state(cfg['state_file'], state)
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='setup',
            status='setup_ready' if ok else 'setup_not_ready',
            setup_time=setup_time.isoformat(),
            message='setup scan completed',
        )
        return

    if not state.get('setup_ready'):
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='skip',
            setup_time=setup_time.isoformat(),
            message='setup not ready',
        )
        return
    if state.get('setup_time') != setup_time.isoformat():
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='skip',
            setup_time=setup_time.isoformat(),
            message='setup time mismatch',
        )
        return

    ew = m1[(m1.index > setup_time) & (m1.index <= next_time)]
    trg = trendpullback_trigger(ew, cfg['params']['breakout_n'], cfg['params']['trigger_ema'])
    if trg is None:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='no_trigger',
            setup_time=setup_time.isoformat(),
            message='no trigger in current window',
        )
        return

    ts, entry = trg
    stop = float(state['setup_low'] - 0.2 * state['setup_atr14'])
    risk = entry - stop
    if risk <= 0:
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='invalid',
            setup_time=setup_time.isoformat(),
            trigger_time=ts.isoformat(),
            entry=entry,
            stop=stop,
            message='risk <= 0',
        )
        return

    t1 = entry + 1.5 * risk
    t2 = entry + 2.2 * risk
    key = build_signal_key(setup_time.isoformat(), ts.isoformat())
    if is_duplicate(state.get('last_signal_key'), key):
        append_signal_log(
            log_file=cfg['log_file'],
            strategy=cfg['strategy'],
            stage='trigger',
            status='duplicate',
            signal_key=key,
            setup_time=setup_time.isoformat(),
            trigger_time=ts.isoformat(),
            entry=entry,
            stop=stop,
            take_profit_1=t1,
            take_profit_2=t2,
            message='duplicate signal ignored',
        )
        return

    text = (
        f"{cfg['strategy']} 訊號\n"
        f"Setup: {setup_time.isoformat()}\n"
        f"Trigger: {ts.isoformat()}\n"
        f"進場: {entry:.1f}\n停損: {stop:.1f}\n停利1: {t1:.1f}\n停利2: {t2:.1f}"
    )
    subprocess.run(['openclaw', 'system', 'event', '--text', text, '--mode', 'now'], check=False)
    append_signal_log(
        log_file=cfg['log_file'],
        strategy=cfg['strategy'],
        stage='trigger',
        status='signal_emitted',
        signal_key=key,
        setup_time=setup_time.isoformat(),
        trigger_time=ts.isoformat(),
        entry=entry,
        stop=stop,
        take_profit_1=t1,
        take_profit_2=t2,
        message='signal sent to event bus',
    )
    state['last_signal_key'] = key
    save_state(cfg['state_file'], state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--profile', required=True)
    ap.add_argument('--stage', required=True, choices=['setup', 'trigger'])
    args = ap.parse_args()

    cfg = json.loads(Path(args.profile).read_text(encoding='utf-8'))
    if cfg['kind'] == 'binance_trendpullback':
        run_btc_trendpullback(args.stage, cfg)
    elif cfg['kind'] == 'shioaji_trendpullback':
        run_tsi_trendpullback(args.stage, cfg)
    else:
        raise SystemExit(f"unsupported kind: {cfg['kind']}")


if __name__ == '__main__':
    main()
