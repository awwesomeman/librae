#!/usr/bin/env python3
"""
Reusable walk-forward runner.
Each window: train-select params -> test once.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Dict, Any, List, Tuple
import json

@dataclass
class WFWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str


_REQUIRED_TEST_KEYS = ("trades", "ann_return", "ann_sharpe", "mdd")


def _score(m: Dict[str, Any]) -> float:
    sharpe = m.get('ann_sharpe') if m.get('ann_sharpe') is not None else -9
    mdd = m.get('mdd') if m.get('mdd') is not None else 1
    return float(sharpe) - 2.0 * float(mdd)


def run_walkforward(
    backtest_fn: Callable[..., Dict[str, Any]],
    windows: List[WFWindow],
    param_grid: List[Dict[str, Any]],
    min_trades_train: int = 10,
) -> Dict[str, Any]:
    results = []
    aggregate_test_returns = []

    for w in windows:
        best = None
        for p in param_grid:
            m = backtest_fn(w.train_start, w.train_end, **p)
            if m.get('trades', 0) < min_trades_train:
                continue
            s = _score(m)
            if best is None or s > best[0]:
                best = (s, p, m)

        if best is None:
            results.append({
                'window': f"{w.test_start}~{w.test_end}",
                'status': 'no_valid_param'
            })
            continue

        _, params, train_m = best
        test_m = backtest_fn(w.test_start, w.test_end, **params)
        missing = [k for k in _REQUIRED_TEST_KEYS if k not in test_m]
        if missing:
            results.append({
                'window': f"{w.test_start}~{w.test_end}",
                'status': 'invalid_result',
                'reason': f"missing required keys: {missing}",
            })
            continue
        results.append({
            'window': f"{w.test_start}~{w.test_end}",
            'params': params,
            'train': train_m,
            'test': test_m,
        })
        if test_m.get('avg_ret') is not None and test_m.get('trades'):
            aggregate_test_returns.append((test_m['avg_ret'], test_m['trades']))

    positive_windows = sum(1 for r in results if isinstance(r, dict) and r.get('test', {}).get('ann_return', -1) is not None and r.get('test', {}).get('ann_return', -1) > 0)
    total_scored = sum(1 for r in results if 'test' in r)

    weighted_avg_ret = None
    if aggregate_test_returns:
        num = sum(a * n for a, n in aggregate_test_returns)
        den = sum(n for _, n in aggregate_test_returns)
        weighted_avg_ret = num / den if den else None

    return {
        'wf_results': results,
        'summary': {
            'windows_total': len(windows),
            'windows_scored': total_scored,
            'positive_windows': positive_windows,
            'weighted_avg_test_ret': weighted_avg_ret,
        }
    }


if __name__ == '__main__':
    print(json.dumps({'msg':'import run_walkforward in strategy scripts'}, ensure_ascii=False, indent=2))
