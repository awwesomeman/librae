#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
import pandas as pd

# Ensure project root is on sys.path for cross-package imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
NAUTILUS_ROOT = Path(__file__).resolve().parents[2] / "nautilus_lab"
if str(NAUTILUS_ROOT) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_ROOT))

from nautilus_lab.backtest import run_strict_protocol, Periods
from nautilus_lab.backtest import run_walkforward, WFWindow
from nautilus_lab.backtest import run_stability
from scripts.etl.core_data_sources import fetch_binance_futures_klines
from scripts.etl.core_features import resample_ohlcv, add_multifactor_features, add_daily_trend_gate, multifactor_score

SYMBOL = "BTCUSDT"


def run_backtest(m1, h1, d1, start, end, th=70, cost=10, bn=3, en=10):
    h = h1[(h1.index >= start) & (h1.index <= end)]
    rets, pos = [], None
    for i in range(70, len(h)-1):
        cur = h.iloc[i]; prev = h.iloc[i-1]; t = h.index[i]; nt = h.index[i+1]
        if pos is not None:
            w = m1[(m1.index > pos['last']) & (m1.index <= t)]
            for _, r in w.iterrows():
                if r.low <= pos['stop']:
                    rets.append((pos['stop']-pos['entry'])/pos['entry'] - cost/10000); pos=None; break
                if (not pos['t1d']) and r.high >= pos['t1']:
                    pos['t1d'] = True; pos['part'] = 0.5*((pos['t1']-pos['entry'])/pos['entry'])
                if r.high >= pos['t2']:
                    rem = 0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part'] + rem*((pos['t2']-pos['entry'])/pos['entry']) - cost/10000); pos=None; break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars'] >= 6 or cur.close < cur.ema20:
                    rets.append((cur.close-pos['entry'])/pos['entry'] - cost/10000); pos=None
                else:
                    pos['last'] = t
        if pos is not None:
            continue

        day = t.floor('D') - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]
        trend_gate = (d.close > d.ema20) and (d.ema20 > d.ema20_prev)
        setup = trend_gate and (abs(cur.low-cur.ema20) <= 0.3*cur.atr14) and (cur.close > cur.open) and (cur.close > prev.high)
        if not setup or multifactor_score(cur) < th:
            continue

        ew = m1[(m1.index > t) & (m1.index <= nt)].copy()
        if len(ew) < max(en+2, bn+2):
            continue
        ew['ema'] = ew['close'].ewm(span=en, adjust=False).mean(); ew['hh'] = ew['high'].rolling(bn).max().shift(1)
        trig = None
        for ts, r in ew.iterrows():
            if np.isnan(r.ema) or np.isnan(r.hh):
                continue
            if r.close > r.hh and r.close > r.ema:
                trig = (ts, float(r.close)); break
        if trig is None:
            continue
        ts, entry = trig
        stop = float(cur.low - 0.2*cur.atr14); risk = entry - stop
        if risk <= 0:
            continue
        pos = {'entry': entry, 'stop': stop, 't1': entry+1.5*risk, 't2': entry+2.2*risk, 'bars': 0, 't1d': False, 'part': 0.0, 'last': ts}

    if not rets:
        return {'trades': 0}
    arr = np.array(rets)
    wins = (arr > 0).sum(); gp = arr[arr > 0].sum(); gl = -arr[arr < 0].sum(); pf = gp/gl if gl > 0 else None
    eq = np.cumprod(1 + arr); peak = np.maximum.accumulate(eq); mdd = float(np.max((peak-eq)/peak))
    years = max((pd.Timestamp(end)-pd.Timestamp(start)).days/365.25, 1e-6)
    ann = float(eq[-1]**(1/years)-1); tpy = len(arr)/years
    vol = float(arr.std(ddof=1)*np.sqrt(tpy)) if len(arr) > 1 else None
    sharpe = float((arr.mean()/arr.std(ddof=1))*np.sqrt(tpy)) if len(arr) > 1 and arr.std(ddof=1) > 0 else None
    return {
        'trades': int(len(arr)), 'win_rate': float(wins/len(arr)), 'avg_ret': float(arr.mean()),
        'pf': float(pf) if pf else None, 'ann_return': ann, 'ann_sharpe': sharpe,
        'ann_vol': vol, 'mdd': mdd, 'equity': float(eq[-1])
    }


def main():
    start_ms = int(datetime(2025,1,1,tzinfo=timezone.utc).timestamp()*1000)
    end_ms = int(datetime.now(tz=timezone.utc).timestamp()*1000)
    m1 = fetch_binance_futures_klines(SYMBOL, '1m', start_ms, end_ms)
    h1 = add_multifactor_features(resample_ohlcv(m1, '60min'))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, '1D'))

    periods = Periods('2025-01-01','2025-07-31','2025-08-01','2025-09-30','2025-10-01', pd.Timestamp.utcnow().strftime('%Y-%m-%d'))

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    param_grid = [{'th': th, 'cost': c, 'bn': bn, 'en': en} for th in [65,70,75] for c in [8,10,12] for bn in [3,5] for en in [10,15]]
    strict = run_strict_protocol(bt_fn, param_grid, periods, min_trades_train=15, cost_stress=[8,10,12,16])

    wf = run_walkforward(bt_fn, [
        WFWindow('2025-01-01','2025-04-30','2025-05-01','2025-06-30'),
        WFWindow('2025-03-01','2025-06-30','2025-07-01','2025-08-31'),
        WFWindow('2025-05-01','2025-08-31','2025-09-01','2025-10-31'),
    ], param_grid, min_trades_train=10)

    chosen = strict.get('chosen_params', {'th': 70, 'cost': 10, 'bn': 3, 'en': 10})
    stability = run_stability(bt_fn, periods.val_start, periods.val_end, [
        {'th': t, 'cost': c, 'bn': b, 'en': e}
        for t in [max(60, int(chosen['th'])-5), int(chosen['th']), min(80, int(chosen['th'])+5)]
        for c in [8,10,12]
        for b in [3,5]
        for e in [10,15]
    ])

    out = {
        'strategy': 'MultiFactorScore_v1.0.0-H1-LS-BTCF',
        'data': 'Binance USDS-M Futures',
        **strict,
        'walkforward': wf,
        'validation_stability': stability,
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
