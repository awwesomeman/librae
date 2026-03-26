"""Backtest protocol runners: strict protocol, walk-forward, stability.

These are the canonical implementations — all strategy scripts should use
these instead of any legacy scripts/backtest/ equivalents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .adapter import generate_run_id, metrics_dict_to_backtest_output
from .scoring import REQUIRED_METRICS_KEYS, score, validate_metrics


# ---------------------------------------------------------------------------
# Strict protocol (train → validation → OOS)
# ---------------------------------------------------------------------------


@dataclass
class Periods:
    train_start: str
    train_end: str
    val_start: str
    val_end: str
    oos_start: str
    oos_end: str


def run_strict_protocol(
    backtest_fn: Callable[..., dict[str, Any]],
    param_grid: list[dict[str, Any]],
    periods: Periods,
    min_trades_train: int = 10,
    cost_stress: list[float] | None = None,
    *,
    strategy: str | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    data_source: str = "unknown",
) -> dict[str, Any]:
    """Strict flow: train-select params, validation health-check, OOS once.

    When strategy/symbol/timeframe are provided, the result includes
    a ``backtest_outputs`` dict mapping sample names to BacktestOutput objects.
    """
    scored = []
    for p in param_grid:
        m = backtest_fn(periods.train_start, periods.train_end, **p)
        validate_metrics(m, "train")
        if m.get("trades", 0) < min_trades_train:
            continue
        scored.append((score(m), p, m))
    scored.sort(key=lambda x: x[0], reverse=True)

    if not scored:
        return {"error": "no_valid_params_on_train"}

    _, best_params, train_metrics = scored[0]

    val_metrics = backtest_fn(periods.val_start, periods.val_end, **best_params)
    validate_metrics(val_metrics, "validation")

    val_cost: dict[str, Any] = {}
    if cost_stress:
        for c in cost_stress:
            p2 = dict(best_params)
            p2["cost"] = c
            val_cost[str(c)] = backtest_fn(periods.val_start, periods.val_end, **p2)

    oos_metrics = backtest_fn(periods.oos_start, periods.oos_end, **best_params)
    validate_metrics(oos_metrics, "oos")

    result: dict[str, Any] = {
        "protocol": "strict: train-select, validation-check, oos-once",
        "chosen_params": best_params,
        "train": train_metrics,
        "validation": val_metrics,
        "validation_cost_stress": val_cost,
        "oos_final_once": oos_metrics,
    }

    if strategy and symbol and timeframe:
        shared_run_id = generate_run_id(strategy, symbol)
        sample_map = {
            "train": (train_metrics, periods.train_start, periods.train_end),
            "validation": (val_metrics, periods.val_start, periods.val_end),
            "oos": (oos_metrics, periods.oos_start, periods.oos_end),
        }
        outputs = {}
        for sample_name, (m, s, e) in sample_map.items():
            outputs[sample_name] = metrics_dict_to_backtest_output(
                m,
                strategy=strategy,
                symbol=symbol,
                timeframe=timeframe,
                start=s,
                end=e,
                data_source=data_source,
                run_id=shared_run_id,
                sample=sample_name,
            )
        result["backtest_outputs"] = outputs

    return result


# ---------------------------------------------------------------------------
# Walk-forward
# ---------------------------------------------------------------------------


@dataclass
class WFWindow:
    train_start: str
    train_end: str
    test_start: str
    test_end: str


def run_walkforward(
    backtest_fn: Callable[..., dict[str, Any]],
    windows: list[WFWindow],
    param_grid: list[dict[str, Any]],
    min_trades_train: int = 10,
) -> dict[str, Any]:
    """Walk-forward analysis: each window train-selects params then tests once."""
    results: list[dict[str, Any]] = []
    aggregate_test_returns: list[tuple[float, int]] = []

    for w in windows:
        best: tuple[float, dict, dict] | None = None
        for p in param_grid:
            m = backtest_fn(w.train_start, w.train_end, **p)
            if m.get("trades", 0) < min_trades_train:
                continue
            s = score(m)
            if best is None or s > best[0]:
                best = (s, p, m)

        if best is None:
            results.append({
                "window": f"{w.test_start}~{w.test_end}",
                "status": "no_valid_param",
            })
            continue

        _, params, _train_m = best
        test_m = backtest_fn(w.test_start, w.test_end, **params)
        try:
            validate_metrics(test_m, f"window {w.test_start}~{w.test_end}")
        except ValueError as exc:
            results.append({
                "window": f"{w.test_start}~{w.test_end}",
                "status": "invalid_result",
                "reason": str(exc),
            })
            continue
        results.append({
            "window": f"{w.test_start}~{w.test_end}",
            "params": params,
            "train": _train_m,
            "test": test_m,
        })
        if test_m.get("avg_ret") is not None and test_m.get("trades"):
            aggregate_test_returns.append((test_m["avg_ret"], test_m["trades"]))

    positive_windows = 0
    total_scored = 0
    for r in results:
        if "test" in r:
            total_scored += 1
            if (r["test"].get("ann_return") or 0) > 0:
                positive_windows += 1

    weighted_avg_ret = None
    if aggregate_test_returns:
        num = sum(a * n for a, n in aggregate_test_returns)
        den = sum(n for _, n in aggregate_test_returns)
        weighted_avg_ret = num / den if den else None

    return {
        "wf_results": results,
        "summary": {
            "windows_total": len(windows),
            "windows_scored": total_scored,
            "positive_windows": positive_windows,
            "weighted_avg_test_ret": weighted_avg_ret,
        },
    }


# ---------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------


def run_stability(
    backtest_fn: Callable[..., dict[str, Any]],
    start: str,
    end: str,
    param_grid: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sweep nearby parameter ranges and summarize robustness."""
    rows: list[dict[str, Any]] = []
    for p in param_grid:
        m = backtest_fn(start, end, **p)
        rows.append({
            "params": p,
            "trades": m.get("trades"),
            "ann_return": m.get("ann_return"),
            "ann_sharpe": m.get("ann_sharpe"),
            "mdd": m.get("mdd"),
            "pf": m.get("pf"),
        })

    valid = [
        r for r in rows if r.get("ann_return") is not None and r.get("mdd") is not None
    ]
    robust_count = sum(1 for r in valid if (r["ann_return"] > 0 and r["mdd"] < 0.15))

    return {
        "stability_results": rows,
        "summary": {
            "cases_total": len(rows),
            "cases_valid": len(valid),
            "robust_cases": robust_count,
            "robust_ratio": (robust_count / len(valid)) if valid else None,
        },
    }
