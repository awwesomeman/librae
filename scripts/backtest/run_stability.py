#!/usr/bin/env python3
"""
Reusable stability test runner.
Sweep nearby parameter ranges and summarize robustness.
"""
from __future__ import annotations
from typing import Callable, Dict, Any, List
import json


def run_stability(
    backtest_fn: Callable[..., Dict[str, Any]],
    start: str,
    end: str,
    param_grid: List[Dict[str, Any]],
) -> Dict[str, Any]:
    rows = []
    for p in param_grid:
        m = backtest_fn(start, end, **p)
        rows.append({
            'params': p,
            'trades': m.get('trades'),
            'ann_return': m.get('ann_return'),
            'ann_sharpe': m.get('ann_sharpe'),
            'mdd': m.get('mdd'),
            'pf': m.get('pf'),
        })

    valid = [r for r in rows if r.get('ann_return') is not None and r.get('mdd') is not None]
    robust_count = sum(1 for r in valid if (r['ann_return'] > 0 and r['mdd'] < 0.15))

    return {
        'stability_results': rows,
        'summary': {
            'cases_total': len(rows),
            'cases_valid': len(valid),
            'robust_cases': robust_count,
            'robust_ratio': (robust_count / len(valid)) if valid else None,
        }
    }


if __name__ == '__main__':
    print(json.dumps({'msg':'import run_stability in strategy scripts'}, ensure_ascii=False, indent=2))
