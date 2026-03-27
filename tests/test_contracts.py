from __future__ import annotations

import json
from pathlib import Path

import pytest

from librae.contracts import (
    MEASUREMENT_SPECS,
    REQUIRED_STRATEGY_CONTEXT_KEYS,
    SCHEMA_VERSION,
    require_keys,
)


def test_schema_required_fields_and_types_are_canonical() -> None:
    signals = MEASUREMENT_SPECS["strategy_signals"]
    assert signals.measurement == "strategy_signals"
    assert [(f.name, f.type_name, f.required) for f in signals.fields] == [
        ("signal_strength", "float", True),
        ("confidence", "float", False),
        ("price", "float", False),
        ("quantity", "float", False),
    ]

    perf = MEASUREMENT_SPECS["strategy_performance"]
    assert [f.name for f in perf.fields] == [
        "total_return",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "trades",
    ]
    assert [f.type_name for f in perf.fields] == ["float", "float", "float", "float", "float", "int"]

    curve = MEASUREMENT_SPECS["perf_equity_curve"]
    assert [f.name for f in curve.fields] == [
        "equity",
        "ret_1d",
        "drawdown",
        "benchmark_equity",
        "benchmark_ret_1d",
    ]
    assert all(tag.name != "" for tag in curve.tags)
    assert all(tag.type_name == "str" for tag in curve.tags)


def test_schema_version_tag_required_in_all_measurements() -> None:
    for spec in MEASUREMENT_SPECS.values():
        required_tags = {t.name for t in spec.tags if t.required}
        assert "schema_version" in required_tags
    assert SCHEMA_VERSION == "1.0.0"


def test_strategy_context_has_required_keys() -> None:
    path = Path(__file__).resolve().parents[1] / "config" / "strategy_context.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload, "strategy_context.json should not be empty"

    for strategy_name, context in payload.items():
        assert isinstance(context, dict), f"{strategy_name} must be an object"
        require_keys(context, REQUIRED_STRATEGY_CONTEXT_KEYS, f"strategy_context[{strategy_name}]")


def test_no_alias_fallback_allowed_for_signal_core_keys() -> None:
    alias_record = {
        "ts": "2026-03-05T04:45:00Z",
        "strategy_id": "s1",
        "instrument_id": "BTCUSDT",
        "tf": "M60",
        "side": "buy",
    }
    with pytest.raises(ValueError):
        require_keys(alias_record, ("timestamp", "strategy", "symbol", "timeframe", "side"), "signal")
