"""Shared test fixtures and helpers."""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv_for_tests() -> None:
    """Load .env from the repo root for local `pytest` runs, without overwriting existing env vars.

    librae itself never does this (see librae/db/__init__.py) — loading .env is an
    application's responsibility, and the test suite is the application here.
    CI sets env vars directly and has no .env file, so this is a no-op there.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = (part.strip() for part in line.split("=", 1))
        if not val.startswith(("'", '"')):
            val = val.split(" #", 1)[0].rstrip()  # strip trailing ` # comment`
        val = val.strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_for_tests()

import pytest  # noqa: E402
from librae.core.run_config import (  # noqa: E402
    AccountConfig,
    ExecutionPolicy,
    RunConfig,
    RuntimePolicy,
)


@pytest.fixture
def live_halt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A live deployment's halt file, which ``build_live_trader`` reads from ``LIBRAE_HALT_FILE``."""
    path = tmp_path / "halt"
    monkeypatch.setenv("LIBRAE_HALT_FILE", str(path))
    return path


def make_test_cfg(**overrides) -> RunConfig:
    """Build a minimal RunConfig for tests."""
    initial_balance = overrides.pop("initial_balance", None)
    runtime = overrides.pop("runtime", None)
    runtime_values = {
        field: overrides.pop(field)
        for field in (
            "poll_seconds",
            "reconciliation_interval_seconds",
            "market_data_workers",
        )
        if field in overrides
    }
    defaults = dict(
        strategy_name="test",
        symbols=["BTCUSDT"],
        timeframe="H1",
        market="crypto",
        data_source="binance_spot",
        account=AccountConfig(currency="USDT", initial_cash=100_000.0),
        mode="sim",
        runtime=runtime or RuntimePolicy(**{"poll_seconds": 0, **runtime_values}),
        params={},
        # Most unit tests isolate sizing/risk behavior from liquidity. Tests
        # for volume participation opt in with an explicit policy.
        execution=ExecutionPolicy(max_bar_volume_participation_rate=None),
    )
    if runtime is not None and runtime_values:
        raise ValueError("pass runtime or runtime fields, not both")
    if initial_balance is not None:
        defaults["account"] = AccountConfig(
            currency="USDT",
            initial_cash=initial_balance,
        )
    defaults.update(overrides)
    return RunConfig(**defaults)


def declare_instruments(
    data,
    *,
    currency: str = "USD",
    instrument_type: str = "spot",
) -> dict[str, dict[str, object]]:
    """Declare every unregistered symbol of a direct-Backtest frame as one instrument type.

    Registered symbols resolve from the registry and are left out.
    """
    from librae.config.symbols import load_symbol_registry

    registry = load_symbol_registry()
    if "symbol" not in data.index.names:
        return {}
    symbols = dict.fromkeys(data.index.get_level_values("symbol"))
    return {
        symbol: {"instrument_type": instrument_type, "currency": currency}
        for symbol in symbols
        if symbol not in registry
    }


def primary_subscriptions_for(
    data,
    timeframe: str | None = None,
    *,
    calendar_id: str = "24/7",
    data_source: str = "test",
    instrument_type: str = "spot",
    session_mode: str = "extended",
):
    """Declare one primary subscription per symbol of a direct-Backtest frame.

    The timeframe is inferred from the symbol with the most bars unless given.
    """
    import pandas as pd
    from librae.core.market_data import MarketDataSubscription
    from librae.core.utils import infer_timeframe

    symbols = list(dict.fromkeys(data.index.get_level_values("symbol")))
    if timeframe is None:
        densest = max(symbols, key=lambda symbol: len(data.xs(symbol, level="symbol")))
        timeframe = infer_timeframe(pd.DatetimeIndex(data.xs(densest, level="symbol").index))
    return tuple(
        MarketDataSubscription(
            symbol=symbol,
            timeframe=timeframe,
            calendar_id=calendar_id,
            session_mode=session_mode,
            data_source=data_source,
            instrument_type=instrument_type,
        )
        for symbol in symbols
    )
