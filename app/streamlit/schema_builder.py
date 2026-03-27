#!/usr/bin/env python3
"""Centralized report schema builder — single source for strategy output composition."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def build_sample_periods(periods) -> Dict[str, Dict[str, str]]:
    """Build canonical sample_periods dict from a Periods dataclass."""
    return {
        'train': {'start': periods.train_start, 'end': periods.train_end},
        'validation': {'start': periods.val_start, 'end': periods.val_end},
        'oos': {'start': periods.oos_start, 'end': periods.oos_end},
    }


def build_cost_settings(
    *,
    fee,
    slippage,
    tax,
    round_trip_cost,
    unit: str,
) -> Dict[str, Any]:
    """Build canonical cost_settings dict.

    unit: "points" or "bps" (or other explicit unit agreed by strategy)
    """
    return {
        'unit': unit,
        'fee': fee,
        'slippage': slippage,
        'tax': tax,
        'round_trip_cost': round_trip_cost,
        'round_trip_cost_text': f'round_trip_cost={round_trip_cost} {unit} (fee={fee}, slippage={slippage}, tax={tax})',
    }


def build_strategy_output(
    *,
    strategy: str,
    instrument: str,
    data: str,
    periods,
    cost_settings: Dict[str, Any],
    strict_result: Dict[str, Any],
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the canonical strategy output dict with deterministic key order.

    Key order: strategy, instrument, data, sample_periods, cost_settings,
    protocol, chosen_params, train, validation, validation_cost_stress,
    oos_final_once, ...extras
    """
    sample_periods = build_sample_periods(periods)

    out: Dict[str, Any] = {}
    out['strategy'] = strategy
    out['instrument'] = instrument
    out['data'] = data
    out['sample_periods'] = sample_periods
    out['cost_settings'] = cost_settings

    # Canonical strict-protocol keys in order
    for key in ('protocol', 'chosen_params', 'train', 'validation',
                'validation_cost_stress', 'oos_final_once'):
        if key in strict_result:
            out[key] = strict_result[key]

    # Any remaining keys from strict_result not yet added
    for key, val in strict_result.items():
        if key not in out:
            out[key] = val

    if extras:
        out.update(extras)

    return out


def _resolve_dot_path(data: Dict[str, Any], path: str) -> bool:
    """Check whether a dot-separated path exists in nested dict."""
    parts = path.split('.')
    cur: Any = data
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return False
        cur = cur[p]
    return True


def validate_against_report_schema(
    output: Dict[str, Any],
    schema_path: str = 'templates/report_schema.json',
) -> None:
    """Validate output against report_schema.json required keys.

    Checks brief_required and full_required paths. Raises ValueError
    listing any missing paths.
    """
    if not os.path.isabs(schema_path):
        base = os.path.join(os.path.dirname(__file__), '..', '..')
        schema_path = os.path.join(base, schema_path)

    with open(schema_path) as f:
        schema = json.load(f)

    missing: List[str] = []
    for key_list_name in ('brief_required', 'full_required'):
        for path in schema.get(key_list_name, []):
            if not _resolve_dot_path(output, path):
                missing.append(path)

    if missing:
        raise ValueError(f"Missing required report paths: {missing}")
