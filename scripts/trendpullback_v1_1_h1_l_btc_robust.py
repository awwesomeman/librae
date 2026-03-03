#!/usr/bin/env python3
import json
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

from run_backtest import run_strict_protocol, Periods
from run_walkforward import run_walkforward, WFWindow
from run_stability import run_stability

BASE = "https://api.binance.com"
SYMBOL = "BTCUSDT"
COST_BPS = 8


def fetch_klines(interval: str, start_ms: int, end_ms: int):
    out, cur = [], start_ms
    while cur < end_ms:
        params = {"symbol": SYMBOL, "interval": interval, "startTime": cur, "endTime": end_ms, "limit": 1000}
        rows = None
        for _ in range(6):
            r = requests.get(f"{BASE}/api/v3/klines", params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.2); continue
            r.raise_for_status(); rows = r.json(); break
        if rows is None:
            raise RuntimeError("rate limit retries exceeded")
        if not rows:
            break
        out.extend(rows)
        cur = int(rows[-1][0]) + 1
        if len(rows) < 1000:
            break
        time.sleep(0.08)
    return out


def to_df(rows):
    df = pd.DataFrame(rows, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","tbv","tqv","ignore"])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df[["ts","open","high","low","close","volume"]].dropna().set_index("ts").sort_index()


def resample_ohlcv(df, rule):
    x = pd.DataFrame()
    x["open"] = df["open"].resample(rule).first()
    x["high"] = df["high"].resample(rule).max()
    x["low"] = df["low"].resample(rule).min()
    x["close"] = df["close"].resample(rule).last()
    x["volume"] = df["volume"].resample(rule).sum()
    return x.dropna()


def add_indicators(df):
    o = df.copy()
    o["ema20"] = o["close"].ewm(span=20, adjust=False).mean()
    tr = pd.concat([
        o["high"] - o["low"],
        (o["high"] - o["close"].shift(1)).abs(),
        (o["low"] - o["close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    o["atr14"] = tr.ewm(alpha=1/14, adjust=False).mean()
    o["vol_sma20"] = o["volume"].rolling(20).mean()
    return o


def run_backtest(m1, h1, d1, start, end, pull=0.3, bn=5, en=20, tstop=6, cost=8):
    h = h1[(h1.index >= start) & (h1.index <= end)]
    rets, pos = [], None
    for i in range(30, len(h)-1):
        cur = h.iloc[i]; prev = h.iloc[i-1]; t = h.index[i]; nt = h.index[i+1]
        if pos is not None:
            w = m1[(m1.index > pos['last']) & (m1.index <= t)]
            for _, r in w.iterrows():
                if r['low'] <= pos['stop']:
                    rets.append((pos['stop']-pos['entry'])/pos['entry'] - cost/10000); pos=None; break
                if (not pos['t1d']) and (r['high'] >= pos['t1']):
                    pos['t1d'] = True; pos['part'] = 0.5*((pos['t1']-pos['entry'])/pos['entry'])
                if r['high'] >= pos['t2']:
                    rem = 0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part'] + rem*((pos['t2']-pos['entry'])/pos['entry']) - cost/10000); pos=None; break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars'] >= tstop or cur['close'] < cur['ema20']:
                    rets.append((cur['close']-pos['entry'])/pos['entry'] - cost/10000); pos=None
                else:
                    pos['last'] = t
        if pos is not None:
            continue

        day = t.floor('D') - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
        near = abs(cur['low'] - cur['ema20']) <= pull*cur['atr14']
        bullish = (cur['close'] > cur['open']) and (cur['close'] > prev['high'])
        vol_ok = (cur['volume'] >= 0.9*cur['vol_sma20']) if not np.isnan(cur['vol_sma20']) else False
        if not (trend and near and bullish and vol_ok and cur['atr14'] > 0):
            continue

        ew = m1[(m1.index > t) & (m1.index <= nt)].copy()
        if len(ew) < max(en+2, bn+2):
            continue
        ew['ema'] = ew['close'].ewm(span=en, adjust=False).mean()
        ew['hh'] = ew['high'].rolling(bn).max().shift(1)
        trg = None
        for ts, r in ew.iterrows():
            if np.isnan(r['ema']) or np.isnan(r['hh']):
                continue
            if r['close'] > r['hh'] and r['close'] > r['ema']:
                trg = (ts, float(r['close'])); break
        if trg is None:
            continue

        ts, entry = trg
        stop = float(cur['low'] - 0.2*cur['atr14'])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {'entry':entry,'stop':stop,'t1':entry+1.5*risk,'t2':entry+2.2*risk,'bars':0,'t1d':False,'part':0.0,'last':ts}

    if not rets:
        return {'trades': 0}
    arr = np.array(rets)
    wins = int((arr > 0).sum()); gp = float(arr[arr > 0].sum()); gl = float(-arr[arr < 0].sum())
    pf = gp / gl if gl > 0 else None
    eq = np.cumprod(1 + arr); peak = np.maximum.accumulate(eq); mdd = float(np.max((peak - eq) / peak))
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-6)
    ann = float(eq[-1]**(1/years) - 1)
    tpy = len(arr) / years
    vol = float(arr.std(ddof=1) * np.sqrt(tpy)) if len(arr) > 1 else None
    sharpe = float((arr.mean()/arr.std(ddof=1))*np.sqrt(tpy)) if len(arr) > 1 and arr.std(ddof=1) > 0 else None
    return {
        'trades': int(len(arr)), 'win_rate': wins/len(arr), 'avg_ret': float(arr.mean()),
        'pf': pf, 'ann_return': ann, 'ann_sharpe': sharpe, 'ann_vol': vol,
        'mdd': mdd, 'equity': float(eq[-1])
    }


def main():
    start_ms = int(datetime(2025,1,1,tzinfo=timezone.utc).timestamp()*1000)
    end_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)
    m1 = to_df(fetch_klines('1m', start_ms, end_ms))
    h1 = add_indicators(resample_ohlcv(m1, '60min'))
    d1 = resample_ohlcv(m1, '1D')
    d1['ema20'] = d1['close'].ewm(span=20, adjust=False).mean(); d1['ema20_prev'] = d1['ema20'].shift(1)

    periods = Periods('2025-01-01', '2025-08-31', '2025-09-01', '2025-10-31', '2025-11-01', pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d'))

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    baseline = {'pull': 0.3, 'bn': 5, 'en': 20, 'tstop': 6, 'cost': COST_BPS}
    grid = [{'pull': pull, 'bn': bn, 'en': en, 'tstop': tstop, 'cost': COST_BPS}
            for pull in [0.25,0.30,0.35] for bn in [3,5] for en in [10,20] for tstop in [4,6]]

    strict = run_strict_protocol(bt_fn, grid, periods, min_trades_train=30, cost_stress=[8,12,16])
    tuned = strict.get('chosen_params', baseline)

    windows = [
        WFWindow('2025-01-01','2025-04-30','2025-05-01','2025-06-30'),
        WFWindow('2025-03-01','2025-06-30','2025-07-01','2025-08-31'),
        WFWindow('2025-05-01','2025-08-31','2025-09-01','2025-10-31'),
    ]
    wf = run_walkforward(bt_fn, windows, grid, min_trades_train=20)

    stability_grid = [
        {'pull': p, 'bn': b, 'en': e, 'tstop': t, 'cost': COST_BPS}
        for p in [max(0.2, tuned['pull']-0.05), tuned['pull'], min(0.4, tuned['pull']+0.05)]
        for b in [3,5] for e in [10,20] for t in [4,6]
    ]
    stability = run_stability(bt_fn, periods.val_start, periods.val_end, stability_grid)

    out = {
        'baseline_name': 'TrendPullback_v1.0-H1-L-BTC',
        'candidate_name': 'TrendPullback_v1.1-H1-L-BTC',
        **strict,
        'validation_baseline': bt_fn(periods.val_start, periods.val_end, **baseline),
        'oos_baseline': bt_fn(periods.oos_start, periods.oos_end, **baseline),
        'oos_tuned': bt_fn(periods.oos_start, periods.oos_end, **tuned),
        'walkforward': wf,
        'validation_stability': stability,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
