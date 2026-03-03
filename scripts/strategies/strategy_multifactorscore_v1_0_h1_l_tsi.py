#!/usr/bin/env python3
"""MultiFactorScore_v1.0-H1-L-TSI baseline — MXFR1 via Shioaji.

Strict flow: Train+Val parameter selection → OOS once.
"""
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from scripts.backtest.run_backtest import run_strict_protocol, Periods
from scripts.etl.core_data_sources import fetch_shioaji_mxfr1_1m
from scripts.etl.core_features import resample_ohlcv, add_multifactor_features, add_daily_trend_gate, multifactor_score
from scripts.reporting.schema_builder import build_cost_settings, build_strategy_output

COST_PTS = 2.0  # round-trip cost in index points


def run_backtest(m1, h1, d1, start, end, cost=2.0, th=75, bn=3, en=10):
    h = h1[(h1.index >= start) & (h1.index <= end)]
    rets, pos = [], None
    for i in range(70, len(h) - 1):
        cur = h.iloc[i]; prev = h.iloc[i - 1]; t = h.index[i]; nt = h.index[i + 1]

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
        setup = trend_gate and (abs(cur['low'] - cur['ema20']) <= 0.3 * cur['atr14']) and (cur['close'] > cur['open']) and (cur['close'] > prev['high'])
        if (not setup) or multifactor_score(cur) < th:
            continue

        ew = m1[(m1.index > t) & (m1.index <= nt)].copy()
        if len(ew) < max(en + 2, bn + 2):
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
        stop = float(cur['low'] - 0.2 * cur['atr14'])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {'entry': entry, 'stop': stop, 't1': entry + 1.5 * risk, 't2': entry + 2.2 * risk,
               'bars': 0, 't1d': False, 'part': 0.0, 'last': ts}

    if not rets:
        return {'trades': 0}
    arr = np.array(rets)
    wins = int((arr > 0).sum()); gp = float(arr[arr > 0].sum()); gl = float(-arr[arr < 0].sum())
    pf = gp / gl if gl > 0 else None
    eq = np.cumprod(1 + arr); peak = np.maximum.accumulate(eq); mdd = float(np.max((peak - eq) / peak))
    years = max((pd.Timestamp(end) - pd.Timestamp(start)).days / 365.25, 1e-6)
    ann = float(eq[-1] ** (1 / years) - 1)
    tpy = len(arr) / years
    vol = float(arr.std(ddof=1) * np.sqrt(tpy)) if len(arr) > 1 else None
    sharpe = float((arr.mean() / arr.std(ddof=1)) * np.sqrt(tpy)) if len(arr) > 1 and arr.std(ddof=1) > 0 else None
    return {
        'trades': int(len(arr)), 'win_rate': float(wins / len(arr)), 'avg_ret': float(arr.mean()),
        'pf': pf, 'equity': float(eq[-1]), 'ann_return': ann, 'ann_sharpe': sharpe,
        'ann_vol': vol, 'mdd': mdd,
    }


def main():
    m1 = fetch_shioaji_mxfr1_1m('2024-01-01', '2026-03-03', simulation=True)
    h1 = add_multifactor_features(resample_ohlcv(m1, '60min'))
    d1 = add_daily_trend_gate(resample_ohlcv(m1, '1D'))

    periods = Periods('2024-01-01', '2025-03-31', '2025-04-01', '2025-06-30', '2025-07-01', '2026-03-03')

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    grid = [{'th': th, 'bn': bn, 'en': en, 'cost': COST_PTS}
            for th in [65, 70, 75, 80] for bn in [3, 5] for en in [10, 15, 20]]

    strict = run_strict_protocol(bt_fn, grid, periods, min_trades_train=15, cost_stress=[2.0, 3.0, 4.0])

    cost_settings = build_cost_settings(
        fee=2.0,
        slippage=0.0,
        tax=0.0,
        round_trip_cost=COST_PTS,
        unit='points',
        notes='小台雙邊交易成本 NT$100 以 2 點等價表示',
    )
    out = build_strategy_output(
        strategy='MultiFactorScore_v1.0-H1-L-TSI',
        instrument='MXFR1',
        data='Shioaji',
        periods=periods,
        cost_settings=cost_settings,
        strict_result=strict,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
