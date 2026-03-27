"""Shared scoring and validation helpers for backtest runners."""
from __future__ import annotations

from typing import Any

REQUIRED_METRICS_KEYS = ("trades", "ann_return", "ann_sharpe", "mdd")


def validate_metrics(m: dict[str, Any], context: str = "") -> None:
    """Raise ValueError if *m* is missing any required backtest metric keys."""
    missing = [k for k in REQUIRED_METRICS_KEYS if k not in m]
    if missing:
        ctx = f" ({context})" if context else ""
        raise ValueError(f"backtest result missing required keys: {missing}{ctx}")


def score(m: dict[str, Any]) -> float:
    """Composite score: sharpe - 2 * mdd.  Used for parameter selection."""
    sharpe = m.get("ann_sharpe") if m.get("ann_sharpe") is not None else -9
    mdd = m.get("mdd") if m.get("mdd") is not None else 1
    return float(sharpe) - 2.0 * float(mdd)
