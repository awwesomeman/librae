#!/usr/bin/env python3
"""
通用回測框架（防資料洩漏版）
流程固定：Train 選參 -> Validation 檢查 -> OOS 單次評估

用法（在策略腳本中 import）：
  from run_backtest import run_strict_protocol
  result = run_strict_protocol(backtest_fn, param_grid, periods)

backtest_fn 介面：
  backtest_fn(start, end, **params) -> dict
  必須回傳至少：trades, ann_return, ann_sharpe, mdd
"""

from __future__ import annotations
import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Any


@dataclass
class Periods:
    train_start: str
    train_end: str
    val_start: str
    val_end: str
    oos_start: str
    oos_end: str


def _score(m: Dict[str, Any]) -> float:
    sharpe = m.get("ann_sharpe") if m.get("ann_sharpe") is not None else -9
    mdd = m.get("mdd") if m.get("mdd") is not None else 1
    return float(sharpe) - 2.0 * float(mdd)


def run_strict_protocol(
    backtest_fn: Callable[..., Dict[str, Any]],
    param_grid: List[Dict[str, Any]],
    periods: Periods,
    min_trades_train: int = 10,
    cost_stress: List[float] | None = None,
) -> Dict[str, Any]:
    """嚴格流程：只用 Train 選參；Validation 做健檢；OOS 僅一次。"""

    # 1) Train 選參
    scored = []
    for p in param_grid:
        m = backtest_fn(periods.train_start, periods.train_end, **p)
        if m.get("trades", 0) < min_trades_train:
            continue
        scored.append((_score(m), p, m))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return {"error": "no_valid_params_on_train"}

    _, best_params, train_metrics = scored[0]

    # 2) Validation（不再選參）
    val_metrics = backtest_fn(periods.val_start, periods.val_end, **best_params)

    val_cost = {}
    if cost_stress:
        for c in cost_stress:
            p2 = dict(best_params)
            p2["cost"] = c
            val_cost[str(c)] = backtest_fn(periods.val_start, periods.val_end, **p2)

    # 3) OOS 單次
    oos_metrics = backtest_fn(periods.oos_start, periods.oos_end, **best_params)

    return {
        "protocol": "strict: train-select, validation-check, oos-once",
        "chosen_params": best_params,
        "train": train_metrics,
        "validation": val_metrics,
        "validation_cost_stress": val_cost,
        "oos_final_once": oos_metrics,
    }


if __name__ == "__main__":
    print(json.dumps({"msg": "Import this module from strategy scripts."}, ensure_ascii=False, indent=2))
