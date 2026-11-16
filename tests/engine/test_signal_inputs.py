"""A strategy's signal_inputs column reaches each signal it explains."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from librae.core.strategy import (
    Context,
    OrderIntent,
    Strategy,
    validate_signal_inputs,
)

from tests.engine.live_factories import _test_cfg, _test_execution_identity, make_live_runner


class TestValidateSignalInputs:
    @pytest.mark.parametrize("missing", [None, float("nan"), pd.NA])
    def test_a_missing_cell_has_no_inputs(self, missing) -> None:
        assert validate_signal_inputs(missing) is None

    def test_scalars_become_plain_json_values(self) -> None:
        inputs = validate_signal_inputs(
            {
                "zscore": np.float64(-2.5),
                "lookback": np.int64(20),
                "trend_up": np.bool_(True),
                "regime": "calm",
                "spread": float("nan"),
                "basis": None,
            }
        )

        assert inputs == {
            "zscore": -2.5,
            "lookback": 20,
            "trend_up": True,
            "regime": "calm",
            "spread": None,
            "basis": None,
        }
        assert all(not isinstance(value, np.generic) for value in inputs.values())

    def test_an_integer_is_stored_exactly_however_large(self) -> None:
        """An int is always finite; turning it into a float would overflow."""
        assert validate_signal_inputs({"volume": 10**400}) == {"volume": 10**400}

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ({"zscore": math.inf}, r"signal_inputs\['zscore'\] is inf; only finite numbers"),
            ({"window": [1.0, 2.0]}, r"signal_inputs\['window'\] must be a number"),
            (
                {"ratio": Fraction(10**400)},
                r"signal_inputs\['ratio'\] does not fit a float; only finite numbers",
            ),
            ({1: 2.0}, "signal_inputs keys must be strings"),
            (2.0, "signal_inputs must be a dict of scalar values, got float"),
        ],
    )
    def test_unstorable_values_are_refused(self, value, message) -> None:
        with pytest.raises((TypeError, ValueError), match=message):
            validate_signal_inputs(value)


def _signalling(inputs: object):
    def features(history: pd.DataFrame) -> pd.DataFrame:
        featured = history.copy()
        featured["entry_signal"] = 0.0
        featured["exit_signal"] = 0.0
        featured.loc[featured.index[-1], ["entry_signal", "exit_signal"]] = [1.0, -1.0]
        if inputs is not None:
            featured["signal_inputs"] = [None] * (len(featured) - 1) + [inputs]
        return featured

    return features


def _flat_order_adapter() -> MagicMock:
    adapter = MagicMock()
    adapter.get_position.return_value = {
        "symbol": "",
        "size": 0,
        "avg_price": 0,
        "unrealized_pnl": 0,
    }
    adapter.get_balance.return_value = {"free": 100_000.0, "used": 0.0, "total": 100_000.0}
    adapter.find_order.return_value = None
    adapter.list_open_orders.return_value = []
    adapter.execution_identity.return_value = _test_execution_identity()
    return adapter


class _Recording(Strategy):
    def __init__(self) -> None:
        self.decided: list[datetime] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        self.decided.append(ctx.ts)
        return []


class _BrokenMapping(Mapping):
    """A dict-like cell whose reading fails in a way validation never anticipates."""

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        raise RuntimeError("broken inputs")

    def __len__(self) -> int:
        return 1


def _recorded(
    mode: str, inputs: object, strategy: Strategy | None = None
) -> list[tuple[str, float, str, object]]:
    runner = make_live_runner(
        strategy=strategy,
        feature_fn=_signalling(inputs),
        config=_test_cfg(mode=mode),
        **({"order_adapter": _flat_order_adapter()} if mode == "live" else {}),
    )
    calls: list[tuple[str, float, str, object]] = []

    def on_signal_outcome(
        symbol: str,
        ts: datetime,
        signal: float,
        price: float,
        *,
        signal_type: str = "entry",
        inputs: object = None,
    ) -> None:
        calls.append((symbol, signal, signal_type, inputs))

    runner._on_signal_outcome = on_signal_outcome
    runner._poll_cycle()
    return calls


@pytest.mark.parametrize("mode", ["sim", "live"])
def test_each_signal_carries_its_bars_inputs(mode: str) -> None:
    calls = _recorded(mode, {"zscore": np.float64(-2.5), "spread": float("nan")})

    inputs = {"zscore": -2.5, "spread": None}
    assert calls == [
        ("BTCUSDT", 1.0, "entry", inputs),
        ("BTCUSDT", -1.0, "exit", inputs),
    ]


def test_a_signal_without_inputs_passes_none() -> None:
    assert [call[3] for call in _recorded("sim", None)] == [None, None]


@pytest.mark.parametrize(
    ("inputs", "logged"),
    [
        ({"zscore": math.inf}, "ValueError: signal_inputs['zscore'] is inf"),
        ({"ratio": Fraction(10**400)}, "ValueError: signal_inputs['ratio'] does not fit"),
        (_BrokenMapping(), "RuntimeError: broken inputs"),
    ],
    ids=["infinite", "overflowing", "unanticipated"],
)
@pytest.mark.parametrize("mode", ["sim", "live"])
def test_failing_inputs_cost_the_record_its_inputs_not_the_cycle(
    mode: str, inputs: object, logged: str, caplog
) -> None:
    strategy = _Recording()

    with caplog.at_level(logging.WARNING, logger="librae.live.engine"):
        calls = _recorded(mode, inputs, strategy)

    assert [call[3] for call in calls] == [None, None]
    assert strategy.decided
    assert "Signal inputs for BTCUSDT" in caplog.text
    assert logged in caplog.text
