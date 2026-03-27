"""Canonical backend data contracts for librae.

Single source of truth file:
- schemas/canonical_schema.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type_name: str
    required: bool
    unit: str


@dataclass(frozen=True)
class TagSpec:
    name: str
    type_name: str
    required: bool


@dataclass(frozen=True)
class MeasurementSpec:
    measurement: str
    tags: tuple[TagSpec, ...]
    fields: tuple[FieldSpec, ...]


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "canonical_schema.json"
SNAKE_CASE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _load_canonical_schema() -> dict[str, Any]:
    if not SCHEMA_PATH.exists():
        raise RuntimeError(f"Canonical schema not found: {SCHEMA_PATH}")
    payload = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("canonical_schema.json must be an object")
    return payload


CANONICAL_SCHEMA = _load_canonical_schema()
SCHEMA_VERSION = str(CANONICAL_SCHEMA["schema_version"])
SCHEMA_COMPATIBILITY_POLICY = dict(CANONICAL_SCHEMA.get("compatibility_policy", {}))


def _build_measurement_specs(schema: dict[str, Any]) -> dict[str, MeasurementSpec]:
    specs: dict[str, MeasurementSpec] = {}
    for measurement, spec in schema.get("measurements", {}).items():
        tags = tuple(
            TagSpec(
                name=str(t["name"]),
                type_name=str(t["type"]),
                required=bool(t.get("required", False)),
            )
            for t in spec.get("tags", [])
        )
        fields = tuple(
            FieldSpec(
                name=str(f["name"]),
                type_name=str(f["type"]),
                required=bool(f.get("required", False)),
                unit=str(f.get("unit", "")),
            )
            for f in spec.get("fields", [])
        )
        specs[measurement] = MeasurementSpec(measurement=measurement, tags=tags, fields=fields)
    return specs


MEASUREMENT_SPECS: dict[str, MeasurementSpec] = _build_measurement_specs(CANONICAL_SCHEMA)

REQUIRED_SIGNAL_KEYS: tuple[str, ...] = tuple(CANONICAL_SCHEMA["records"]["signal_required_keys"])
REQUIRED_SUMMARY_KEYS: tuple[str, ...] = tuple(CANONICAL_SCHEMA["records"]["strategy_context_summary_required_keys"])
REQUIRED_PERF_FIELDS: tuple[str, ...] = tuple(CANONICAL_SCHEMA["records"]["performance_required_fields"])
REQUIRED_STRATEGY_CONTEXT_KEYS: tuple[str, ...] = tuple(CANONICAL_SCHEMA["records"]["strategy_context_required_keys"])
REQUIRED_BACKTEST_TOP_LEVEL_KEYS: tuple[str, ...] = tuple(CANONICAL_SCHEMA["records"]["backtest_output_required_top_level"])


def parse_utc_timestamp(value: Any) -> datetime:
    if isinstance(value, str) and value:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e18:
            ts = ts / 1e9
        elif ts > 1e15:
            ts = ts / 1e6
        elif ts > 1e12:
            ts = ts / 1e3
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    raise ValueError("timestamp must be ISO8601 string or epoch number")


def ensure_snake_case_keys(keys: list[str] | tuple[str, ...], record_name: str) -> None:
    invalid = [k for k in keys if not SNAKE_CASE_PATTERN.match(str(k))]
    if invalid:
        raise ValueError(f"{record_name} has non-snake_case keys: {invalid}")


def require_keys(record: dict[str, Any], keys: tuple[str, ...], record_name: str) -> None:
    missing = [k for k in keys if k not in record or record[k] in (None, "")]
    if missing:
        raise ValueError(f"{record_name} missing required keys: {missing}")


def validate_record_contract(record: dict[str, Any], required_keys: tuple[str, ...], record_name: str) -> None:
    ensure_snake_case_keys(list(record.keys()), record_name)
    require_keys(record, required_keys, record_name)


def validate_signal_record(record: dict[str, Any]) -> None:
    validate_record_contract(record, REQUIRED_SIGNAL_KEYS, "strategy_signals record")


def validate_dataframe_columns(df: pd.DataFrame, required: set[str], dataset: str) -> None:
    if df.empty:
        return
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{dataset} missing required columns: {', '.join(missing)}")


def validate_perf_fields(perf_raw: pd.DataFrame) -> None:
    if perf_raw.empty:
        return
    # Column-based format: each metric is a column in the DataFrame
    columns = set(perf_raw.columns)
    missing = sorted(set(REQUIRED_PERF_FIELDS) - columns)
    if missing:
        raise ValueError(f"strategy_performance missing required fields: {', '.join(missing)}")


def validate_strategy_context(record: dict[str, Any], record_name: str) -> None:
    validate_record_contract(record, REQUIRED_STRATEGY_CONTEXT_KEYS, record_name)
    summary = record.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{record_name}.summary must be an object")
    validate_record_contract(summary, REQUIRED_SUMMARY_KEYS, f"{record_name}.summary")


def is_schema_compatible(payload_schema_version: str, expected_schema_version: str = SCHEMA_VERSION) -> bool:
    """Backward compatibility policy: only major version must match."""
    try:
        payload_major = int(str(payload_schema_version).split(".")[0])
        expected_major = int(str(expected_schema_version).split(".")[0])
        return payload_major == expected_major
    except Exception:
        return False
