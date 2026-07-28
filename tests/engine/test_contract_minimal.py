from __future__ import annotations

import pytest
from librae.backtest.schema import (
    REQUIRED_BACKTEST_TOP_LEVEL_KEYS,
    ensure_snake_case_keys,
    require_keys,
)
from librae.core.executor import partition_pending_decision, validate_strategy_decision
from librae.core.strategy import MultiLegOrder, OrderIntent


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


def test_multi_leg_decision_waits_for_a_synchronous_market_event() -> None:
    decision = MultiLegOrder(
        legs=(
            OrderIntent(action="long", symbol="NEAR", quantity=1.0),
            OrderIntent(action="short", symbol="NEXT", quantity=1.0),
        )
    )
    validate_strategy_decision(decision, {"NEAR", "NEXT"}, primary_symbol="NEAR")

    ready, waiting = partition_pending_decision(
        decision,
        {"NEAR": {"close": 100.0}},
        {},
        primary_symbol="NEAR",
    )
    assert ready == []
    assert waiting == decision

    ready, waiting = partition_pending_decision(
        decision,
        {"NEAR": {"close": 100.0}, "NEXT": {"close": 101.0}},
        {},
        primary_symbol="NEAR",
    )
    assert ready == decision
    assert waiting == []
