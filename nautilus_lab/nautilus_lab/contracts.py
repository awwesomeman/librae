"""Canonical backend data contracts for nautilus_lab.

Single source of truth for:
- InfluxDB measurement schema
- strategy_context required keys
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "1.0.0"


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


MEASUREMENT_SPECS: dict[str, MeasurementSpec] = {
    "strategy_signals": MeasurementSpec(
        measurement="strategy_signals",
        tags=(
            TagSpec("schema_version", "str", True),
            TagSpec("strategy", "str", True),
            TagSpec("symbol", "str", True),
            TagSpec("timeframe", "str", True),
            TagSpec("side", "str", True),
            TagSpec("source", "str", True),
            TagSpec("run_id", "str", True),
            TagSpec("signal_type", "str", True),
        ),
        fields=(
            FieldSpec("signal_strength", "float", True, "score"),
            FieldSpec("confidence", "float", False, "ratio_0_1"),
            FieldSpec("price", "float", False, "quote_ccy"),
            FieldSpec("quantity", "float", False, "contract_or_asset_qty"),
        ),
    ),
    "strategy_performance": MeasurementSpec(
        measurement="strategy_performance",
        tags=(
            TagSpec("schema_version", "str", True),
            TagSpec("strategy", "str", True),
            TagSpec("symbol", "str", True),
            TagSpec("timeframe", "str", True),
            TagSpec("run_id", "str", True),
            TagSpec("sample", "str", True),
            TagSpec("benchmark", "str", True),
        ),
        fields=(
            FieldSpec("total_return", "float", True, "ratio"),
            FieldSpec("annual_return", "float", True, "ratio"),
            FieldSpec("sharpe", "float", True, "score"),
            FieldSpec("max_drawdown", "float", True, "ratio"),
            FieldSpec("win_rate", "float", True, "ratio_0_1"),
            FieldSpec("trades", "int", True, "count"),
        ),
    ),
    "perf_equity_curve": MeasurementSpec(
        measurement="perf_equity_curve",
        tags=(
            TagSpec("schema_version", "str", True),
            TagSpec("strategy", "str", True),
            TagSpec("symbol", "str", True),
            TagSpec("timeframe", "str", True),
            TagSpec("run_id", "str", True),
            TagSpec("sample", "str", True),
            TagSpec("benchmark", "str", True),
        ),
        fields=(
            FieldSpec("equity", "float", True, "index"),
            FieldSpec("ret_1d", "float", True, "ratio"),
            FieldSpec("drawdown", "float", True, "ratio"),
            FieldSpec("benchmark_equity", "float", True, "index"),
            FieldSpec("benchmark_ret_1d", "float", True, "ratio"),
        ),
    ),
}

REQUIRED_SIGNAL_KEYS: tuple[str, ...] = ("timestamp", "strategy", "symbol", "side", "timeframe")

REQUIRED_STRATEGY_CONTEXT_KEYS: tuple[str, ...] = (
    "benchmark",
    "data_source",
    "data_version",
    "last_updated_utc",
    "summary",
    "universe",
    "session_rules",
    "periods",
    "cost_model",
    "risk_limits",
    "assumptions",
    "logic",
    "params",
)


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


def require_keys(record: dict[str, Any], keys: tuple[str, ...], record_name: str) -> None:
    missing = [k for k in keys if k not in record or record[k] in (None, "")]
    if missing:
        raise ValueError(f"{record_name} missing required keys: {missing}")


def validate_signal_record(record: dict[str, Any]) -> None:
    require_keys(record, REQUIRED_SIGNAL_KEYS, "strategy_signals record")
