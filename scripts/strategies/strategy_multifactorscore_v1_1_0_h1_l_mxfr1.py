#!/usr/bin/env python3
import os
import sys
import json
from pathlib import Path
import numpy as np
import pandas as pd

# Ensure project root is on sys.path for cross-package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
NAUTILUS_ROOT = Path(__file__).resolve().parents[2] / "nautilus_lab"
if str(NAUTILUS_ROOT) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_ROOT))

from nautilus_lab.backtest import run_strict_protocol, Periods
from nautilus_lab.backtest import run_stability
from scripts.etl.core_data_sources import fetch_shioaji_mxfr1_1m
from scripts.etl.core_features import resample_ohlcv, add_multifactor_features, add_daily_trend_gate, multifactor_score

def run_backtest(m1, h1, d1, start, end, cost=2.0, th=75, bn=3, en=10):
    h = h1[(h1.index >= start) & (h1.index <= end)]
    rets = []
    pos = None
    for i in range(70, len(h)-1):
        cur = h.iloc[i]; prev = h.iloc[i-1]; t = h.index[i]; nt = h.index[i+1]

        if pos is not None:
            w = m1[(m1.index > pos['last']) & (m1.index <= t)]
            for _, r in w.iterrows():
                if r['low'] <= pos['stop']:
                    rets.append((pos['stop'] - pos['entry']) / pos['entry'] - cost / pos['entry']); pos = None; break
                if (not pos['t1d']) and r['high'] >= pos['t1']:
                    pos['t1d'] = True; pos['part'] = 0.5 * ((pos['t1'] - pos['entry']) / pos['entry'])
                if r['high'] >= pos['t2']:
                    rem = 0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part'] + rem * ((pos['t2'] - pos['entry']) / pos['entry']) - cost / pos['entry']); pos = None; break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars'] >= 6 or cur['close'] < cur['ema20']:
                    rets.append((cur['close'] - pos['entry']) / pos['entry'] - cost / pos['entry']); pos = None
                else:
                    pos['last'] = t
        if pos is not None:
            continue

        day = t.floor('D') - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend_gate = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
        setup = trend_gate and (abs(cur['low'] - cur['ema20']) <= 0.3*cur['atr14']) and (cur['close'] > cur['open']) and (cur['close'] > prev['high'])
        if (not setup) or multifactor_score(cur) < th:
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
                trg = (ts, float(r['close']))
                break
        if trg is None:
            continue

        ts, entry = trg
        stop = float(cur['low'] - 0.2*cur['atr14'])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {'entry': entry, 'stop': stop, 't1': entry + 1.5*risk, 't2': entry + 2.2*risk, 'bars': 0, 't1d': False, 'part': 0.0, 'last': ts}

    if not rets:
        return {'trades': 0}
    arr = np.array(rets)
    wins = (arr > 0).sum(); gp = arr[arr > 0].sum(); gl = -arr[arr < 0].sum()
    pf = gp / gl if gl > 0 else np.nan
    eq = np.cumprod(1 + arr); peak = np.maximum.accumulate(eq); mdd = np.max((peak - eq) / peak)
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-6)
    ann = eq[-1]**(1/years) - 1; tpy = len(arr) / years
    vol = arr.std(ddof=1) * np.sqrt(tpy) if len(arr) > 1 else np.nan
    sharpe = (arr.mean() / arr.std(ddof=1)) * np.sqrt(tpy) if len(arr) > 1 and arr.std(ddof=1) > 0 else np.nan
    return {
        'trades': int(len(arr)), 'win_rate': float(wins / len(arr)), 'avg_ret': float(arr.mean()),
        'pf': float(pf) if not np.isnan(pf) else None, 'ann_return': float(ann),
        'ann_sharpe': float(sharpe) if not np.isnan(sharpe) else None,
        'ann_vol': float(vol) if not np.isnan(vol) else None, 'mdd': float(mdd), 'equity': float(eq[-1])
    }


def main():
    m1 = fetch_shioaji_mxfr1_1m('2024-01-01', '2026-03-02', simulation=True)
    h1 = add_multifactor_features(resample_ohlcv(m1, '60min'))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, '1D'))

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    periods = Periods('2024-01-01', '2025-03-31', '2025-04-01', '2025-06-30', '2025-07-01', '2026-03-02')
    param_grid = [{'th': th, 'bn': bn, 'en': en, 'cost': 2.0} for th in [65,70,75,80] for bn in [3,5] for en in [10,15,20]]
    result = run_strict_protocol(bt_fn, param_grid, periods, min_trades_train=15, cost_stress=[2.0,3.0,4.0])

    chosen = result.get('chosen_params', {})
    stable_grid = [
        {'th': th2, 'bn': bn2, 'en': en2, 'cost': 2.0}
        for th2 in [max(60, int(chosen.get('th', 70))-5), int(chosen.get('th', 70)), min(85, int(chosen.get('th', 70))+5)]
        for bn2 in [3,5]
        for en2 in [10,15,20]
    ]
    stability = run_stability(bt_fn, periods.val_start, periods.val_end, stable_grid)

    out = {
        'strategy': 'MultiFactorScore_v1.1.0-H1-L-MXFR1',
        **result,
        'validation_stability': stability.get('stability_results', []),
        'validation_stability_summary': stability.get('summary', {}),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
