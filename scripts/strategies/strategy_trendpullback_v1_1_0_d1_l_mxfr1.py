#!/usr/bin/env python3
"""TrendPullback_v1.1.0-D1-L-MXFR1 — D1 decision + H1 trigger for MXFR1.

Decision timeframe : D1 (daily bar qualifies trend-aligned pullback setup).
Trigger timeframe  : H1 (hourly bar breakout fires the entry).
Exit management    : 1m precision for stop-loss and profit targets.

Strict flow: Train+Val parameter selection → OOS once.
"""
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
NAUTILUS_ROOT = Path(__file__).resolve().parents[2] / "quant_lab"
if str(NAUTILUS_ROOT) not in sys.path:
    sys.path.insert(0, str(NAUTILUS_ROOT))

from quant_lab.backtest import run_strict_protocol, Periods
from scripts.etl.core_data_sources import fetch_shioaji_mxfr1_1m
from scripts.etl.core_features import resample_ohlcv, add_trendpullback_features
from scripts.reporting.schema_builder import build_cost_settings, build_strategy_output

STRATEGY_NAME = 'TrendPullback_v1.1.0-D1-L-MXFR1'
COST_PTS = 2.0  # round-trip cost in index points


def _prepare_d1(m1: pd.DataFrame) -> pd.DataFrame:
    """D1 bars: EMA20, ATR14, vol_sma20 (from add_trendpullback_features) + ema20_prev."""
    d1 = add_trendpullback_features(resample_ohlcv(m1, '1D'))
    d1['ema20_prev'] = d1['ema20'].shift(1)
    return d1


def run_backtest(m1, h1, d1, start, end, pull=0.3, bn=5, en=20, tstop=6, cost=2.0):
    """D1-decision + H1-trigger TrendPullback backtest.

    Parameters
    ----------
    m1    : 1-minute OHLCV DataFrame (tz-aware UTC index)
    h1    : H1 OHLCV with EMA20 / ATR14 / vol_sma20 features
    d1    : D1 OHLCV with EMA20 / ATR14 / vol_sma20 / ema20_prev features
    pull  : pullback proximity factor (fraction of D1 ATR14)
    bn    : H1 rolling-high lookback bars for trigger
    en    : EMA span on H1 bars for trigger
    tstop : max H1 bars held before time-stop exit
    cost  : round-trip cost in index points
    """
    h = h1[(h1.index >= start) & (h1.index <= end)]
    rets, pos = [], None

    for i in range(max(30, en + bn + 5), len(h) - 1):
        cur = h.iloc[i]
        t = h.index[i]

        # ── Exit management via 1m precision ─────────────────────────────────
        if pos is not None:
            w = m1[(m1.index > pos['last']) & (m1.index <= t)]
            for _, r in w.iterrows():
                if r['low'] <= pos['stop']:
                    rets.append((pos['stop'] - pos['entry']) / pos['entry'] - cost / pos['entry'])
                    pos = None; break
                if (not pos['t1d']) and (r['high'] >= pos['t1']):
                    pos['t1d'] = True
                    pos['part'] = 0.5 * ((pos['t1'] - pos['entry']) / pos['entry'])
                if r['high'] >= pos['t2']:
                    rem = 0.5 if pos['t1d'] else 1.0
                    rets.append(pos['part'] + rem * ((pos['t2'] - pos['entry']) / pos['entry']) - cost / pos['entry'])
                    pos = None; break
            if pos is not None:
                pos['bars'] += 1
                if pos['bars'] >= tstop or cur['close'] < cur['ema20']:
                    rets.append((cur['close'] - pos['entry']) / pos['entry'] - cost / pos['entry'])
                    pos = None
                else:
                    pos['last'] = t

        if pos is not None:
            continue

        # ── D1 Decision: previous trading day's daily bar ────────────────────
        day = t.floor('D') - pd.Timedelta(days=1)
        if day not in d1.index:
            continue
        d = d1.loc[day]

        trend = (d['close'] > d['ema20']) and (d['ema20'] > d['ema20_prev'])
        near = abs(d['low'] - d['ema20']) <= pull * d['atr14']
        bullish = d['close'] > d['open']
        vol_ok = (d['volume'] >= 0.9 * d['vol_sma20']) if not np.isnan(d['vol_sma20']) else False
        if not (trend and near and bullish and vol_ok and d['atr14'] > 0):
            continue

        # ── H1 Trigger: current H1 bar breaks above rolling high and EMA ─────
        h_slice = h1.iloc[max(0, i - en - bn - 5): i + 1]
        ema_h1 = float(h_slice['close'].ewm(span=en, adjust=False).mean().iloc[-1])
        hh_h1 = float(h_slice['high'].rolling(bn).max().shift(1).iloc[-1])
        if np.isnan(ema_h1) or np.isnan(hh_h1):
            continue
        if not (cur['close'] > hh_h1 and cur['close'] > ema_h1):
            continue

        entry = float(cur['close'])
        stop = float(d['low'] - 0.2 * d['atr14'])
        risk = entry - stop
        if risk <= 0:
            continue
        pos = {
            'entry': entry, 'stop': stop,
            't1': entry + 1.5 * risk, 't2': entry + 2.2 * risk,
            'bars': 0, 't1d': False, 'part': 0.0, 'last': t,
        }

    if not rets:
        return {'trades': 0}
    arr = np.array(rets)
    wins = int((arr > 0).sum())
    gp = float(arr[arr > 0].sum())
    gl = float(-arr[arr < 0].sum())
    pf = gp / gl if gl > 0 else None
    eq = np.cumprod(1 + arr)
    peak = np.maximum.accumulate(eq)
    mdd = float(np.max((peak - eq) / peak))
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


# ── Synthetic data helper (offline report generation / CI) ────────────────────

def _make_synthetic_m1(days: int = 500, seed: int = 7) -> pd.DataFrame:
    """Deterministic synthetic 1-minute OHLCV with upward drift."""
    rng = np.random.default_rng(seed)
    n = days * 24 * 60
    idx = pd.date_range('2024-01-01', periods=n, freq='1min', tz='UTC')
    log_ret = rng.normal(0.0008 / 1440, 0.00035, n)
    close = 20_000.0 * np.exp(np.cumsum(log_ret))
    noise = rng.uniform(0.0002, 0.0012, n)
    high = close * (1.0 + noise)
    low = close * (1.0 - noise)
    open_ = np.empty(n); open_[0] = close[0]; open_[1:] = close[:-1]
    high = np.maximum(high, open_); low = np.minimum(low, open_)
    volume = rng.uniform(100.0, 500.0, n)
    return pd.DataFrame(
        {'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume},
        index=idx,
    )


def run_with_synthetic() -> dict:
    """Run full strict protocol on synthetic data; returns output dict."""
    m1 = _make_synthetic_m1()
    h1 = add_trendpullback_features(resample_ohlcv(m1, '60min'))
    d1 = _prepare_d1(m1)

    # Derive periods from actual data extent so all splits land within bounds.
    first_ts = d1.index[0]
    total_days = (d1.index[-1] - first_ts).days
    # 60 / 20 / 20 split
    train_end = (first_ts + pd.Timedelta(days=int(total_days * 0.60))).strftime('%Y-%m-%d')
    val_start = (first_ts + pd.Timedelta(days=int(total_days * 0.60) + 1)).strftime('%Y-%m-%d')
    val_end   = (first_ts + pd.Timedelta(days=int(total_days * 0.80))).strftime('%Y-%m-%d')
    oos_start = (first_ts + pd.Timedelta(days=int(total_days * 0.80) + 1)).strftime('%Y-%m-%d')
    oos_end   = d1.index[-1].strftime('%Y-%m-%d')
    periods = Periods(first_ts.strftime('%Y-%m-%d'), train_end, val_start, val_end, oos_start, oos_end)

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    grid = [{'pull': p, 'bn': b, 'en': e, 'tstop': t, 'cost': COST_PTS}
            for p in [0.25, 0.30, 0.35] for b in [3, 5] for e in [10, 20] for t in [4, 6]]

    strict = run_strict_protocol(bt_fn, grid, periods, min_trades_train=5, cost_stress=[2.0, 3.0, 4.0])

    cost_settings = build_cost_settings(
        fee=2.0,
        slippage=0.0,
        tax=0.0,
        round_trip_cost=COST_PTS,
        unit='points',
    )
    return build_strategy_output(
        strategy=STRATEGY_NAME,
        instrument='MXFR1',
        data='Shioaji (synthetic)',
        periods=periods,
        cost_settings=cost_settings,
        strict_result=strict,
    )


def main():
    m1 = fetch_shioaji_mxfr1_1m('2024-01-01', '2026-03-03', simulation=True)
    h1 = add_trendpullback_features(resample_ohlcv(m1, '60min'))
    d1 = _prepare_d1(m1)

    periods = Periods('2024-01-01', '2025-03-31', '2025-04-01', '2025-06-30', '2025-07-01', '2026-03-03')

    def bt_fn(start, end, **params):
        return run_backtest(m1, h1, d1, start, end, **params)

    grid = [{'pull': p, 'bn': b, 'en': e, 'tstop': t, 'cost': COST_PTS}
            for p in [0.25, 0.30, 0.35] for b in [3, 5] for e in [10, 20] for t in [4, 6]]

    strict = run_strict_protocol(bt_fn, grid, periods, min_trades_train=10, cost_stress=[2.0, 3.0, 4.0])

    cost_settings = build_cost_settings(
        fee=2.0,
        slippage=0.0,
        tax=0.0,
        round_trip_cost=COST_PTS,
        unit='points',
    )
    out = build_strategy_output(
        strategy=STRATEGY_NAME,
        instrument='MXFR1',
        data='Shioaji',
        periods=periods,
        cost_settings=cost_settings,
        strict_result=strict,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
