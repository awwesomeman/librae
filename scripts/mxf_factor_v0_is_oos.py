#!/usr/bin/env python3
import os, json
import numpy as np
import pandas as pd
import shioaji as sj

COST_POINTS = 2.0  # MTX round-trip


def resample(df, rule):
    x = pd.DataFrame()
    x['open'] = df['open'].resample(rule).first()
    x['high'] = df['high'].resample(rule).max()
    x['low'] = df['low'].resample(rule).min()
    x['close'] = df['close'].resample(rule).last()
    x['volume'] = df['volume'].resample(rule).sum()
    return x.dropna()


def add_h1_features(h1):
    o = h1.copy()
    o['ema20'] = o['close'].ewm(span=20, adjust=False).mean()
    o['ema60'] = o['close'].ewm(span=60, adjust=False).mean()
    tr = pd.concat([
        o['high'] - o['low'],
        (o['high'] - o['close'].shift(1)).abs(),
        (o['low'] - o['close'].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o['atr14'] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    o['vol_sma20'] = o['volume'].rolling(20).mean()
    o['ret20'] = o['close'].pct_change(20)
    o['ret60'] = o['close'].pct_change(60)
    o['atrp'] = o['atr14'] / o['close']

    # structure health proxies
    roll_max = o['close'].rolling(40).max()
    o['dd40'] = (roll_max - o['close']) / roll_max
    o['hh'] = (o['high'] > o['high'].shift(1)).astype(float)
    o['hl'] = (o['low'] > o['low'].shift(1)).astype(float)
    return o


def factor_score(row):
    # F1 trend 30%
    trend = 0.0
    if row['close'] > row['ema20'] > row['ema60']:
        trend = 100.0
    elif row['close'] > row['ema20']:
        trend = 60.0
    else:
        trend = 20.0

    # F2 momentum 25%
    mom = 50.0
    if not np.isnan(row['ret20']) and not np.isnan(row['ret60']):
        mom_raw = 0.6 * row['ret20'] + 0.4 * row['ret60']
        mom = np.clip(50 + mom_raw * 1000, 0, 100)

    # F3 structure 20%
    dd = np.clip(row['dd40'] if not np.isnan(row['dd40']) else 0.2, 0, 0.2)
    dd_score = 100 * (1 - dd / 0.2)
    structure = np.clip(0.7 * dd_score + 30 * row['hh'] + 30 * row['hl'], 0, 100)

    # F4 volume quality 15%
    if np.isnan(row['vol_sma20']) or row['vol_sma20'] <= 0:
        vol = 40.0
    else:
        vr = row['volume'] / row['vol_sma20']
        vol = np.clip(vr * 70, 0, 100)

    # F5 volatility penalty 10% (low vol higher)
    if np.isnan(row['atrp']):
        vp = 50.0
    else:
        vp = np.clip(100 - row['atrp'] * 5000, 0, 100)

    total = 0.30 * trend + 0.25 * mom + 0.20 * structure + 0.15 * vol + 0.10 * vp
    return total


def backtest(m1, h1f, d1, start, end, threshold=65, breakout_n=5, ema_n=20):
    h = h1f[(h1f.index >= start) & (h1f.index <= end)].copy()
    if len(h) < 100:
        return {'trades': 0}

    trades = []
    pos = None

    for i in range(70, len(h) - 1):
        cur = h.iloc[i]
        prev = h.iloc[i - 1]
        t = h.index[i]
        nt = h.index[i + 1]

        # manage open pos on 1m path
        if pos is not None:
            w = m1[(m1.index > pos['last']) & (m1.index <= t)]
            for _, r in w.iterrows():
                if r['low'] <= pos['stop']:
                    net_points = (pos['stop'] - pos['entry']) - COST_POINTS
                    trades.append(net_points / pos['entry'])
                    pos = None
                    break
                if (not pos['t1d']) and (r['high'] >= pos['t1']):
                    pos['t1d'] = True
                    pos['part'] = 0.5 * ((pos['t1'] - pos['entry']) / pos['entry'])
                if r['high'] >= pos['t2']:
                    rem = 0.5 if pos['t1d'] else 1.0
                    gross = pos['part'] + rem * ((pos['t2'] - pos['entry']) / pos['entry'])
                    # cost in return space
                    gross -= COST_POINTS / pos['entry']
                    trades.append(gross)
                    pos = None
                    break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars'] >= 6 or cur['close'] < cur['ema20']:
                    net_points = (cur['close'] - pos['entry']) - COST_POINTS
                    trades.append(net_points / pos['entry'])
                    pos = None
                else:
                    pos['last'] = t

        if pos is not None:
            continue

        day = t.floor('D') - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend_gate = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
        setup = trend_gate and (abs(cur['low'] - cur['ema20']) <= 0.3 * cur['atr14']) and (cur['close'] > cur['open']) and (cur['close'] > prev['high'])
        if not setup:
            continue

        sc = factor_score(cur)
        if sc < threshold:
            continue

        ew = m1[(m1.index > t) & (m1.index <= nt)].copy()
        if len(ew) < max(ema_n + 2, breakout_n + 2):
            continue
        ew['ema'] = ew['close'].ewm(span=ema_n, adjust=False).mean()
        ew['hh'] = ew['high'].rolling(breakout_n).max().shift(1)

        trigger = None
        for ts, r in ew.iterrows():
            if np.isnan(r['ema']) or np.isnan(r['hh']):
                continue
            if r['close'] > r['hh'] and r['close'] > r['ema']:
                trigger = (ts, float(r['close']))
                break
        if trigger is None:
            continue

        ts, entry = trigger
        stop = float(cur['low'] - 0.2 * cur['atr14'])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {
            'entry': entry,
            'stop': stop,
            't1': entry + 1.5 * risk,
            't2': entry + 2.2 * risk,
            'bars': 0,
            't1d': False,
            'part': 0.0,
            'last': ts,
        }

    if len(trades) == 0:
        return {'trades': 0}

    arr = np.array(trades)
    wins = (arr > 0).sum()
    gp = arr[arr > 0].sum()
    gl = -arr[arr < 0].sum()
    pf = gp / gl if gl > 0 else np.nan

    eq = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(eq)
    mdd = np.max((peak - eq) / peak)

    years = (pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25
    ann = (eq[-1] ** (1 / years) - 1) if years > 0 else np.nan
    tpy = len(arr) / years if years > 0 else np.nan
    vol = arr.std(ddof=1) * np.sqrt(tpy) if len(arr) > 1 else np.nan
    sharpe = (arr.mean() / arr.std(ddof=1) * np.sqrt(tpy)) if len(arr) > 1 and arr.std(ddof=1) > 0 else np.nan

    return {
        'trades': int(len(arr)),
        'win_rate': float(wins / len(arr)),
        'avg_ret': float(arr.mean()),
        'pf': float(pf),
        'mdd': float(mdd),
        'equity': float(eq[-1]),
        'ann_return': float(ann),
        'ann_sharpe': float(sharpe) if not np.isnan(sharpe) else np.nan,
        'ann_vol': float(vol) if not np.isnan(vol) else np.nan,
    }


def main():
    key = os.getenv('SINO_API_KEY'); sec = os.getenv('SINO_SECRET_KEY')
    api = sj.Shioaji(simulation=True)
    api.login(api_key=key, secret_key=sec)
    kb = api.kbars(api.Contracts.Futures.MXF.MXFR1, start='2024-01-01', end='2026-03-02')
    api.logout()

    df = pd.DataFrame({**kb})
    df['ts'] = pd.to_datetime(df['ts'])
    df = df.rename(columns={'Open': 'open', 'High': 'high', 'Low': 'low', 'Close': 'close', 'Volume': 'volume'}).set_index('ts').sort_index()

    m1 = df[['open', 'high', 'low', 'close', 'volume']]
    h1 = add_h1_features(resample(m1, '60min'))
    d1 = resample(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean()
    d1['ema20_prev'] = d1['ema20'].shift(1)

    train_s, train_e = '2024-01-01', '2025-03-31'
    val_s, val_e = '2025-04-01', '2025-06-30'
    oos_s, oos_e = '2025-07-01', '2026-03-02'

    candidates = []
    for th in [55, 60, 65, 70, 75]:
        for bn in [3, 5, 8]:
            for en in [10, 20]:
                m = backtest(m1, h1, d1, val_s, val_e, th, bn, en)
                if m.get('trades', 0) < 8:
                    continue
                score = (m['ann_sharpe'] if not np.isnan(m['ann_sharpe']) else -9) - 3.0 * m['mdd']
                candidates.append((score, th, bn, en, m))

    candidates.sort(key=lambda x: x[0], reverse=True)
    best = candidates[0] if candidates else None
    if best is None:
        print(json.dumps({'error': 'no valid params'}, ensure_ascii=False, indent=2))
        return

    _, th, bn, en, val_m = best
    train_m = backtest(m1, h1, d1, train_s, train_e, th, bn, en)
    oos_m = backtest(m1, h1, d1, oos_s, oos_e, th, bn, en)

    out = {
        'chosen_params': {'threshold': th, 'breakout_n': bn, 'trigger_ema_n': en},
        'train': train_m,
        'validation': val_m,
        'oos': oos_m,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
