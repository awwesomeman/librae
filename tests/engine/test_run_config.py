from __future__ import annotations

import pytest
from librae.core.run_config import (
    AccountConfig,
    ExecutionPolicy,
    ReportingPolicy,
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
        "accounts": {"default": AccountConfig(currency="USD", initial_cash=10_000.0)},
        "mode": "backtest",
        "params": {"window": 20, "nested": {"enabled": True}},
    }
    values.update(overrides)
    return RunConfig(**values)


def test_config_detaches_and_freezes_nested_inputs() -> None:
    symbols = ["AAA", "BBB"]
    params = {"window": 20, "nested": {"enabled": True}}
    cfg = _config(symbols=symbols, params=params)
    original_hash = cfg.config_hash

    symbols.reverse()
    params["window"] = 99
    params["nested"]["enabled"] = False

    assert cfg.symbols == ("AAA", "BBB")
    assert cfg.params == {"window": 20, "nested": {"enabled": True}}
    assert cfg.config_hash == original_hash
    with pytest.raises(TypeError, match="immutable"):
        cfg.params["window"] = 10
    with pytest.raises(TypeError, match="immutable"):
        cfg.params["nested"]["enabled"] = False


def test_config_hash_preserves_primary_symbol_order_and_mode() -> None:
    backtest = _config()
    reordered = _config(symbols=["BBB", "AAA"])
    simulation = _config(mode="sim")

    assert backtest.config_hash != reordered.config_hash
    assert backtest.config_hash != simulation.config_hash


def test_run_owns_exactly_one_account() -> None:
    with pytest.raises(ValueError, match="exactly one account"):
        _config(
            accounts={
                "a": AccountConfig(currency="USD", initial_cash=10_000.0),
                "b": AccountConfig(currency="USD", initial_cash=10_000.0),
            }
        )


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


def test_reporting_policy_is_validated_and_excluded_from_config_hash() -> None:
    default = _config()
    weekly = _config(
        reporting=ReportingPolicy(
            annualize=False,
            risk_free_rate=0.01,
            periods_per_year=52,
        )
    )

    assert weekly.perf_params == {
        "annualize": False,
        "risk_free_rate": 0.01,
        "periods_per_year": 52,
    }
    assert weekly.config_hash == default.config_hash


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
    short_warmup = _config(execution=ExecutionPolicy(warmup_periods=10))

    assert unlimited.config_hash != capped.config_hash
    assert capped.config_hash != adv_capped.config_hash
    assert capped.config_hash != timed_live_order.config_hash
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
    with pytest.raises(ValueError, match="bar field"):
        ExecutionPolicy(default_fill_price="")
    with pytest.raises(TypeError, match="ExecutionPolicy"):
        _config(execution={"max_bar_volume_participation_rate": 0.1})


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


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"symbols": "AAA"}, "symbols"),
        ({"symbols": ["AAA", 1]}, "symbols"),
        ({"strategy_name": 1}, "strategy_name"),
        ({"broker": ""}, "broker"),
        ({"accounts": True}, "accounts"),
        ({"risk_free_rate": True}, "risk_free_rate"),
        ({"risk_free_rate": -1.0}, "risk_free_rate"),
        ({"annualize": 1}, "annualize"),
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
