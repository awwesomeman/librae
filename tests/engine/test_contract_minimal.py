from __future__ import annotations

import pytest

from librae.backtest.schema import (
    REQUIRED_BACKTEST_TOP_LEVEL_KEYS,
    ensure_snake_case_keys,
    require_keys,
)


def test_contract_fails_when_required_field_missing() -> None:
    payload = {
        "run_metadata": {},
        "equity_curve": [],
        "metrics": {},
    }
    with pytest.raises(ValueError, match="missing required keys"):
        require_keys(payload, REQUIRED_BACKTEST_TOP_LEVEL_KEYS, "backtest_output")


def test_contract_fails_on_naming_mismatch() -> None:
    payload = {
        "runMetadata": {},
        "equity_curve": [],
        "trades": [],
        "metrics": {},
    }
    with pytest.raises(ValueError, match="missing required keys"):
        require_keys(payload, REQUIRED_BACKTEST_TOP_LEVEL_KEYS, "backtest_output")


def test_contract_fails_on_non_snake_case_keys() -> None:
    with pytest.raises(ValueError, match="non-snake_case"):
        ensure_snake_case_keys(["run_metadata", "runMetadata", "equity_curve"], "backtest_output")
