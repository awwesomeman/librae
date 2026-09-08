from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from librae.core.run_config import (
    AccountConfig,
    ExecutionPolicy,
    RiskPolicy,
    RunConfig,
    RuntimePolicy,
)


def _config(**overrides: object) -> RunConfig:
    values: dict[str, object] = {
        "strategy_name": "test",
        "symbols": ["AAA", "BBB"],
        "timeframe": "H1",
        "market": "crypto",
        "data_source": "test",
        "account": AccountConfig(currency="USD", initial_cash=10_000.0),
        "mode": "backtest",
        "params": {"window": 20, "nested": {"enabled": True}},
    }
    values.update(overrides)
    return RunConfig(**values)


def test_config_detaches_and_freezes_nested_inputs() -> None:
    symbols = ["AAA", "BBB"]
    windows = [5, 20]
    params = {"window": 20, "windows": windows, "nested": {"enabled": True}}
    cfg = _config(symbols=symbols, params=params)
    original_hash = cfg.config_hash

    symbols.reverse()
    windows.append(60)
    params["window"] = 99
    params["nested"]["enabled"] = False

    assert cfg.symbols == ("AAA", "BBB")
    assert cfg.params == {
        "window": 20,
        "windows": (5, 20),
        "nested": {"enabled": True},
    }
    assert cfg.config_hash == original_hash
    with pytest.raises(TypeError, match="immutable"):
        cfg.params["window"] = 10
    with pytest.raises(TypeError, match="immutable"):
        cfg.params["nested"]["enabled"] = False


def test_equal_json_like_content_has_the_same_hash() -> None:
    first = _config(params={"b": [1, 2], "a": {"rate": 0.1}})
    reordered = _config(params={"a": {"rate": 0.1}, "b": (1, 2)})

    assert first.config_hash == reordered.config_hash


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (True, 1),
        (1, 1.0),
        (1.0, "0x1.0000000000000p+0"),
        (None, "null"),
    ],
)
def test_config_hash_preserves_scalar_type_distinctions(left: object, right: object) -> None:
    assert (
        _config(params={"value": left}).config_hash != _config(params={"value": right}).config_hash
    )


@pytest.mark.parametrize(
    "value",
    [
        np.array([1.0]),
        {1, 2},
        bytearray(b"mutable"),
        object(),
    ],
)
def test_config_rejects_non_canonical_mapping_values(value: object) -> None:
    with pytest.raises(TypeError, match="unsupported"):
        _config(params={"value": value})


def test_config_rejects_non_string_mapping_keys_and_non_finite_floats() -> None:
    with pytest.raises(TypeError, match="mapping keys must be strings"):
        _config(params={1: "value"})
    with pytest.raises(ValueError, match="finite"):
        _config(params={"value": float("nan")})


def test_config_rejects_non_mapping_top_level_values() -> None:
    with pytest.raises(TypeError, match="params must be a dictionary"):
        _config(params=[("window", 20)])


def test_config_hash_is_stable_across_processes() -> None:
    config = _config(params={"z": [1, 0.5, None], "a": {"enabled": True}})
    script = """
from librae.core.run_config import AccountConfig, RunConfig

config = RunConfig(
    strategy_name="test",
    symbols=["AAA", "BBB"],
    timeframe="H1",
    market="crypto",
    data_source="test",
    account=AccountConfig(currency="USD", initial_cash=10_000.0),
    mode="backtest",
    params={"a": {"enabled": True}, "z": [1, 0.5, None]},
)
print(config.config_hash)
"""
    environment = {**os.environ, "PYTHONHASHSEED": "random"}

    child_hash = subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        text=True,
    ).strip()

    assert child_hash == config.config_hash


def test_config_hash_preserves_primary_symbol_order_and_mode() -> None:
    backtest = _config()
    reordered = _config(symbols=["BBB", "AAA"])
    simulation = _config(mode="sim")

    assert backtest.config_hash != reordered.config_hash
    assert backtest.config_hash != simulation.config_hash


def test_run_config_normalizes_and_validates_timeframe() -> None:
    canonical = _config(timeframe="H6")
    ccxt = _config(timeframe="6h")

    assert canonical.timeframe == "H6"
    assert ccxt.timeframe == "H6"
    assert canonical.config_hash == ccxt.config_hash
    for timeframe in ("M0", "H0", "0m", "0h"):
        with pytest.raises(ValueError, match="positive integer"):
            _config(timeframe=timeframe)


def test_run_requires_account_config() -> None:
    with pytest.raises(TypeError, match="AccountConfig"):
        _config(account={"currency": "USD", "initial_cash": 10_000.0})


def test_runtime_operational_settings_are_validated_but_do_not_change_config_hash() -> None:
    default = _config()
    tuned = _config(
        runtime=RuntimePolicy(
            reconciliation_interval_seconds=30,
            market_data_workers=4,
        )
    )

    assert tuned.runtime.reconciliation_interval_seconds == 30
    assert tuned.runtime.market_data_workers == 4
    assert tuned.config_hash == default.config_hash
    for field in ("reconciliation_interval_seconds", "market_data_workers"):
        with pytest.raises(ValueError, match=field):
            RuntimePolicy(**{field: 0})
        with pytest.raises(ValueError, match=field):
            RuntimePolicy(**{field: True})


def test_execution_policy_is_validated_and_part_of_config_hash() -> None:
    unlimited = _config(execution=ExecutionPolicy(max_bar_volume_participation_rate=None))
    capped = _config(execution=ExecutionPolicy(max_bar_volume_participation_rate=0.1))
    adv_capped = _config(
        timeframe="D1",
        execution=ExecutionPolicy(
            adv_lookback_sessions=20,
            max_adv_participation_rate=0.01,
        ),
    )
    timed_live_order = _config(
        execution=ExecutionPolicy(live_order_timeout_seconds=120),
    )
    delayed_rebalance = _config(
        execution=ExecutionPolicy(max_rebalance_delay_bars=2),
    )
    sliced_rebalance = _config(
        execution=ExecutionPolicy(
            max_rebalance_delay_bars=2,
            rebalance_residual_policy="defer_symbols",
        ),
    )
    short_warmup = _config(execution=ExecutionPolicy(warmup_periods=10))

    assert unlimited.config_hash != capped.config_hash
    assert capped.config_hash != adv_capped.config_hash
    assert capped.config_hash != timed_live_order.config_hash
    assert capped.config_hash != delayed_rebalance.config_hash
    assert delayed_rebalance.config_hash != sliced_rebalance.config_hash
    assert capped.config_hash != short_warmup.config_hash
    with pytest.raises(ValueError, match="must be in"):
        ExecutionPolicy(max_bar_volume_participation_rate=1.1)
    with pytest.raises(ValueError, match="positive integer"):
        ExecutionPolicy(adv_lookback_sessions=0, max_adv_participation_rate=0.01)
    with pytest.raises(ValueError, match="configured together"):
        ExecutionPolicy(adv_lookback_sessions=20)
    with pytest.raises(ValueError, match="live_order_timeout_seconds"):
        ExecutionPolicy(live_order_timeout_seconds=0)
    with pytest.raises(ValueError, match="live_order_timeout_seconds"):
        ExecutionPolicy(live_order_timeout_seconds=True)
    with pytest.raises(ValueError, match="max_rebalance_delay_bars"):
        ExecutionPolicy(max_rebalance_delay_bars=-1)
    with pytest.raises(ValueError, match="max_rebalance_delay_bars"):
        ExecutionPolicy(max_rebalance_delay_bars=True)
    with pytest.raises(ValueError, match="rebalance_residual_policy"):
        ExecutionPolicy(rebalance_residual_policy="retry")
    with pytest.raises(ValueError, match="requires positive"):
        ExecutionPolicy(rebalance_residual_policy="defer_all")
    with pytest.raises(ValueError, match="warmup_periods"):
        ExecutionPolicy(warmup_periods=0)
    with pytest.raises(ValueError, match="warmup_periods"):
        ExecutionPolicy(warmup_periods=True)
    intraday_adv = _config(
        execution=ExecutionPolicy(
            adv_lookback_sessions=20,
            max_adv_participation_rate=0.01,
        )
    )
    assert intraday_adv.execution.adv_lookback_sessions == 20
    with pytest.raises(ValueError, match="causal next-bar"):
        ExecutionPolicy(default_fill_price="")
    for unsupported in ("close", "high", "low", "feature_price"):
        with pytest.raises(ValueError, match="causal next-bar"):
            ExecutionPolicy(default_fill_price=unsupported)
    with pytest.raises(TypeError, match="ExecutionPolicy"):
        _config(execution={"max_bar_volume_participation_rate": 0.1})


def test_execution_policy_preserves_existing_positional_argument_order() -> None:
    policy = ExecutionPolicy("open", 0.2, 10, 0.03, 4, 90, 1_440)

    assert policy.default_fill_price == "open"
    assert policy.max_bar_volume_participation_rate == 0.2
    assert policy.adv_lookback_sessions == 10
    assert policy.max_adv_participation_rate == 0.03
    assert policy.max_rebalance_delay_bars == 4
    assert policy.live_order_timeout_seconds == 90
    assert policy.warmup_periods == 1_440
    assert policy.rebalance_residual_policy == "discard"


def test_risk_policy_is_validated_and_part_of_config_hash() -> None:
    disabled = _config()
    limited = _config(risk=RiskPolicy(max_drawdown_rate=0.2))

    assert disabled.config_hash != limited.config_hash
    with pytest.raises(ValueError, match="max_position_weight"):
        RiskPolicy(max_position_weight=0)
    with pytest.raises(ValueError, match="max_limit_price_deviation_rate"):
        RiskPolicy(max_limit_price_deviation_rate=1.01)
    with pytest.raises(TypeError, match="RiskPolicy"):
        _config(risk={"max_drawdown_rate": 0.2})


@pytest.mark.parametrize("residual_policy", ["discard", "defer_symbols"])
def test_rebalance_delay_is_not_supported_by_sim(residual_policy: str) -> None:
    with pytest.raises(ValueError, match="only when mode='backtest'"):
        _config(
            mode="sim",
            execution=ExecutionPolicy(
                max_rebalance_delay_bars=1,
                rebalance_residual_policy=residual_policy,
            ),
        )


def test_live_accepts_bounded_target_delay_but_not_backtest_slicing_policy() -> None:
    config = _config(
        mode="live",
        execution=ExecutionPolicy(max_rebalance_delay_bars=1),
    )

    assert config.execution.max_rebalance_delay_bars == 1
    with pytest.raises(ValueError, match="rebalance_residual_policy"):
        _config(
            mode="live",
            execution=ExecutionPolicy(
                max_rebalance_delay_bars=1,
                rebalance_residual_policy="defer_symbols",
            ),
        )


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"symbols": "AAA"}, "symbols"),
        ({"symbols": ["AAA", 1]}, "symbols"),
        ({"strategy_name": 1}, "strategy_name"),
        ({"broker": ""}, "broker"),
        ({"calendar_id": ""}, "calendar_id"),
        ({"account": True}, "account"),
    ],
)
def test_run_config_rejects_ambiguous_scalar_types(override, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _config(**override)


@pytest.mark.parametrize(
    "legacy_key",
    [
        "fill_price",
        "max_volume_participation_pct",
        "max_bar_volume_participation_rate",
        "adv_lookback_sessions",
        "max_adv_participation_rate",
        "max_rebalance_delay_bars",
        "rebalance_residual_policy",
        "live_order_timeout_seconds",
        "warmup_periods",
    ],
)
def test_execution_settings_are_rejected_from_strategy_params(
    legacy_key: str,
) -> None:
    with pytest.raises(ValueError, match=r"RunConfig\.execution"):
        _config(params={legacy_key: 0.1})


@pytest.mark.parametrize(
    "legacy_key",
    [
        "max_position_pct",
        "max_drawdown_pct",
        "max_gross_exposure_pct",
        "max_net_exposure_pct",
        "max_position_weight",
        "max_drawdown_rate",
        "max_gross_exposure",
        "max_net_exposure",
        "max_order_notional",
        "max_limit_price_deviation_rate",
    ],
)
def test_risk_settings_are_rejected_from_strategy_params(legacy_key: str) -> None:
    with pytest.raises(ValueError, match=r"RunConfig\.risk"):
        _config(params={legacy_key: 0.1})


@pytest.mark.parametrize("initial_cash", [0.0, -1.0, float("nan")])
def test_initial_cash_must_be_positive_and_finite(initial_cash: float) -> None:
    with pytest.raises(ValueError, match="initial_cash"):
        AccountConfig(currency="USD", initial_cash=initial_cash)
