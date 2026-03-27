from __future__ import annotations

import re

import pytest

from librae.contracts import (
    REQUIRED_BACKTEST_TOP_LEVEL_KEYS,
    SCHEMA_PATH,
    SCHEMA_VERSION,
    ensure_snake_case_keys,
    require_keys,
)


def test_canonical_schema_file_exists_and_version_set() -> None:
    assert SCHEMA_PATH.exists(), f"Missing canonical schema file: {SCHEMA_PATH}"
    assert re.match(r"^\d+\.\d+\.\d+$", SCHEMA_VERSION)


def test_contract_fails_when_required_field_missing() -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_metadata": {},
        "equity_curve": [],
        "metrics": {},
    }
    with pytest.raises(ValueError, match="missing required keys"):
        require_keys(payload, REQUIRED_BACKTEST_TOP_LEVEL_KEYS, "backtest_output")


def test_contract_fails_on_naming_mismatch() -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "runMetadata": {},
        "equity_curve": [],
        "trades": [],
        "metrics": {},
    }
    with pytest.raises(ValueError, match="missing required keys"):
        require_keys(payload, REQUIRED_BACKTEST_TOP_LEVEL_KEYS, "backtest_output")


def test_contract_fails_on_non_snake_case_keys() -> None:
    with pytest.raises(ValueError, match="non-snake_case"):
        ensure_snake_case_keys(["schema_version", "runMetadata", "equity_curve"], "backtest_output")
