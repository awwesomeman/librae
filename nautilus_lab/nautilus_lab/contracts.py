"""Canonical backend data contracts for nautilus_lab.

Single source of truth for:
- InfluxDB measurement schema
- strategy_context required keys
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

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
REQUIRED_SUMMARY_KEYS: tuple[str, ...] = (
    "full_sample_period",
    "train_period",
    "oos_period",
    "asset",
    "freq",
)
REQUIRED_PERF_FIELDS: tuple[str, ...] = (
    "total_return",
    "max_drawdown",
    "profit_factor",
    "win_rate",
    "avg_trade_return",
    "trades",
    "exposure_ratio",
    "bh_total_return",
)

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


def validate_dataframe_columns(df: pd.DataFrame, required: set[str], dataset: str) -> None:
    if df.empty:
        return
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{dataset} missing required columns: {', '.join(missing)}")


def validate_perf_fields(perf_raw: pd.DataFrame) -> None:
    if perf_raw.empty:
        return
    fields = {str(v) for v in perf_raw.get("_field", pd.Series(dtype=str)).dropna().tolist()}
    missing = sorted(set(REQUIRED_PERF_FIELDS) - fields)
    if missing:
        raise ValueError(f"strategy_performance missing required fields: {', '.join(missing)}")


def validate_strategy_context(record: dict[str, Any], record_name: str) -> None:
    require_keys(record, REQUIRED_STRATEGY_CONTEXT_KEYS, record_name)
    summary = record.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{record_name}.summary must be an object")
    missing_summary = [k for k in REQUIRED_SUMMARY_KEYS if summary.get(k) in (None, "")]
    if missing_summary:
        raise ValueError(f"{record_name}.summary missing required keys: {missing_summary}")
