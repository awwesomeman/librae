"""Backward-compat shim — real module is librae.backtest.schema."""
from librae.backtest.schema import (  # noqa: F401
    SCHEMA_VERSION,
    SNAKE_CASE_PATTERN,
    REQUIRED_SIGNAL_KEYS,
    REQUIRED_SUMMARY_KEYS,
    REQUIRED_PERF_FIELDS,
    REQUIRED_STRATEGY_CONTEXT_KEYS,
    REQUIRED_BACKTEST_TOP_LEVEL_KEYS,
    ensure_snake_case_keys,
    is_schema_compatible,
    parse_utc_timestamp,
    require_keys,
    validate_dataframe_columns,
    validate_perf_fields,
    validate_record_contract,
    validate_signal_record,
    validate_strategy_context,
)
