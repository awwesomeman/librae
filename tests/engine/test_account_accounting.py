"""Single-account run-boundary tests."""

from __future__ import annotations

import pytest
from librae import AccountConfig, RunConfig
from librae.config.symbols import resolve_symbol


def _config(**overrides: object) -> RunConfig:
    values: dict[str, object] = {
        "strategy_name": "single_account",
        "symbols": ("AAA", "BBB"),
        "timeframe": "H1",
        "market": "test",
        "data_source": "binance_spot",
        "account": AccountConfig(
            account_id="primary",
            currency="USD",
            initial_cash=1_000.0,
        ),
        "mode": "backtest",
        "symbol_cost_overrides": {
            "AAA": {"multiplier": 1.0},
            "BBB": {"multiplier": 1.0},
        },
        "instrument_overrides": {
            "AAA": {
                "currency": "USD",
                "instrument_type": "spot",
                "data_adapter": "crypto",
            },
            "BBB": {
                "currency": "USD",
                "instrument_type": "spot",
                "data_adapter": "crypto",
            },
        },
    }
    values.update(overrides)
    return RunConfig(**values)


def test_account_id_must_be_non_empty() -> None:
    with pytest.raises(ValueError, match="account_id"):
        AccountConfig(account_id="", currency="USD", initial_cash=1_000.0)


def test_single_account_has_direct_accessors() -> None:
    config = _config()

    assert config.account_id == "primary"
    assert config.account == AccountConfig(
        account_id="primary",
        currency="USD",
        initial_cash=1_000.0,
    )


def test_symbol_cannot_route_to_another_account() -> None:
    config = _config(
        instrument_overrides={
            "AAA": {
                "account_id": "secondary",
                "currency": "USD",
                "instrument_type": "spot",
                "data_adapter": "crypto",
            },
            "BBB": {
                "currency": "USD",
                "instrument_type": "spot",
                "data_adapter": "crypto",
            },
        }
    )

    with pytest.raises(ValueError, match="account_id is not supported"):
        resolve_symbol(config, "AAA")
