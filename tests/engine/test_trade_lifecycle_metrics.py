"""Position-lifecycle and trade excursion metric contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from librae.backtest.schema import (
    BacktestOutput,
    OrderEventRecord,
    RunMetadata,
    StrategyMetrics,
)
from librae.core.metrics import (
    compute_trade_entry_outcomes,
    compute_trade_lifecycle_outcomes,
    generate_trade_tearsheet,
    summarize_trade_entry_outcomes,
    summarize_trade_lifecycle_outcomes,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    event_id: str,
    hour: int,
    event_type: str,
    *,
    symbol: str = "X",
    side: str = "long",
    fill_quantity: float = 1.0,
    price: float = 100.0,
    entry_price: float = 100.0,
    remaining_quantity: float = 1.0,
    pnl: float | None = None,
    periods_held: int | None = None,
) -> OrderEventRecord:
    return OrderEventRecord(
        event_id=event_id,
        ts=T0 + timedelta(hours=hour),
        symbol=symbol,
        side=side,
        event_type=event_type,
        fill_quantity=fill_quantity,
        price=price,
        entry_price=entry_price,
        remaining_quantity=remaining_quantity,
        notional=price * fill_quantity,
        pnl=pnl,
        periods_held=periods_held,
    )


def _ohlcv(
    *,
    periods: int = 8,
    base: float = 100.0,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    index = pd.date_range(T0, periods=periods, freq="1h")
    high_values = highs or [base] * periods
    low_values = lows or [base] * periods
    close_values = np.clip([base] * periods, low_values, high_values)
    return pd.DataFrame(
        {
            "open": [base] * periods,
            "high": high_values,
            "low": low_values,
            "close": close_values,
        },
        index=index,
    )


def test_trade_outcome_functions_are_public() -> None:
    import librae

    assert librae.compute_trade_lifecycle_outcomes is compute_trade_lifecycle_outcomes
    assert librae.summarize_trade_lifecycle_outcomes is summarize_trade_lifecycle_outcomes
    assert librae.compute_trade_entry_outcomes is compute_trade_entry_outcomes
    assert librae.summarize_trade_entry_outcomes is summarize_trade_entry_outcomes


def test_interleaved_symbols_reconstruct_independent_lifecycles() -> None:
    events = [
        _event("a-open", 0, "open", symbol="A"),
        _event(
            "b-open",
            0,
            "open",
            symbol="B",
            price=200.0,
            entry_price=200.0,
        ),
        _event(
            "a-close",
            3,
            "close",
            symbol="A",
            price=105.0,
            remaining_quantity=0.0,
            pnl=5.0,
            periods_held=3,
        ),
        _event(
            "b-close",
            3,
            "close",
            symbol="B",
            price=190.0,
            entry_price=200.0,
            remaining_quantity=0.0,
            pnl=-10.0,
            periods_held=3,
        ),
    ]
    a = _ohlcv(
        highs=[500, 110, 105, 500, 100, 100, 100, 100], lows=[1, 90, 95, 1, 100, 100, 100, 100]
    )
    b = _ohlcv(
        base=200.0,
        highs=[900, 210, 205, 900, 200, 200, 200, 200],
        lows=[1, 180, 190, 1, 200, 200, 200, 200],
    )

    outcomes = compute_trade_lifecycle_outcomes(events, {"A": a, "B": b})

    assert list(outcomes["symbol"]) == ["A", "B"]
    assert list(outcomes["status"]) == ["complete", "complete"]
    assert np.allclose(outcomes["mfe"], [10.0, 5.0])
    assert np.allclose(outcomes["mae"], [10.0, 10.0])
    assert list(outcomes["net_pnl"]) == [5.0, -10.0]


def test_scale_in_basis_applies_prospectively_and_reductions_aggregate() -> None:
    events = [
        _event(
            "open",
            0,
            "open",
            fill_quantity=2.0,
            remaining_quantity=2.0,
        ),
        _event(
            "add",
            2,
            "add",
            fill_quantity=2.0,
            price=200.0,
            entry_price=150.0,
            remaining_quantity=4.0,
        ),
        _event(
            "reduce-1",
            4,
            "reduce",
            price=180.0,
            entry_price=150.0,
            remaining_quantity=3.0,
            pnl=30.0,
        ),
        _event(
            "reduce-2",
            5,
            "reduce",
            price=160.0,
            entry_price=150.0,
            remaining_quantity=2.0,
            pnl=10.0,
        ),
        _event(
            "close",
            6,
            "close",
            fill_quantity=2.0,
            price=120.0,
            entry_price=150.0,
            remaining_quantity=0.0,
            pnl=-40.0,
            periods_held=6,
        ),
    ]
    frame = _ohlcv(
        highs=[100, 110, 200, 180, 180, 160, 120, 100],
        lows=[100, 90, 200, 105, 180, 105, 120, 100],
    )

    outcome = compute_trade_lifecycle_outcomes(events, {"X": frame}).iloc[0]

    assert outcome["status"] == "complete"
    assert outcome["realized_exits"] == 3
    assert outcome["periods_held"] == 6
    assert np.isclose(outcome["net_pnl"], 0.0)
    assert np.isclose(outcome["mfe"], 100.0)
    # The low of 90 happened under the original 100 basis (10% adverse), not
    # retrospectively under the later 150 basis (40% adverse).
    assert np.isclose(outcome["mae"], 30.0)


def test_short_excursion_uses_direction_and_excludes_event_bar_ranges() -> None:
    events = [
        _event("open", 0, "open", side="short"),
        _event(
            "close",
            3,
            "close",
            side="short",
            price=90.0,
            remaining_quantity=0.0,
            pnl=10.0,
            periods_held=3,
        ),
    ]
    frame = _ohlcv(
        highs=[1_000, 110, 105, 1_000, 100, 100, 100, 100],
        lows=[1, 80, 85, 1, 100, 100, 100, 100],
    )

    outcome = compute_trade_lifecycle_outcomes(events, {"X": frame}).iloc[0]

    assert np.isclose(outcome["mfe"], 20.0)
    assert np.isclose(outcome["mae"], 10.0)


def test_partial_broker_fills_use_open_then_add_without_new_event_type() -> None:
    events = [
        _event(
            "partial-1",
            0,
            "open",
            fill_quantity=4.0,
            remaining_quantity=4.0,
        ),
        _event(
            "partial-2",
            0,
            "add",
            fill_quantity=6.0,
            price=110.0,
            entry_price=106.0,
            remaining_quantity=10.0,
        ),
        _event(
            "close",
            2,
            "close",
            fill_quantity=10.0,
            price=112.0,
            entry_price=106.0,
            remaining_quantity=0.0,
            pnl=60.0,
            periods_held=2,
        ),
    ]

    outcome = compute_trade_lifecycle_outcomes(events, {"X": _ohlcv()}).iloc[0]

    assert outcome["status"] == "complete"
    assert outcome["realized_exits"] == 1
    assert outcome["net_pnl"] == 60.0


def test_incomplete_lifecycle_is_surfaced_and_excluded_from_summary() -> None:
    events = [
        _event("complete-open", 0, "open"),
        _event(
            "complete-close",
            2,
            "close",
            remaining_quantity=0.0,
            pnl=0.0,
            periods_held=2,
        ),
        _event("incomplete-open", 3, "open"),
    ]

    outcomes = compute_trade_lifecycle_outcomes(events, {"X": _ohlcv()})
    summary = summarize_trade_lifecycle_outcomes(outcomes)

    assert list(outcomes["status"]) == ["complete", "incomplete"]
    assert pd.isna(outcomes.iloc[1]["closed_at"])
    assert summary.loc[summary["scope"] == "pooled", "n"].item() == 1


def test_entry_outcomes_include_each_open_add_anchor_and_horizon_counts() -> None:
    events = [
        _event("first-open", 0, "open"),
        _event(
            "first-close",
            2,
            "close",
            remaining_quantity=0.0,
            pnl=0.0,
            periods_held=2,
        ),
        _event("second-open", 4, "open"),
        _event(
            "second-add",
            5,
            "add",
            price=100.0,
            entry_price=100.0,
            remaining_quantity=2.0,
        ),
    ]
    frame = _ohlcv(periods=7)

    outcomes = compute_trade_entry_outcomes(events, {"X": frame}, max_periods=3)
    summary = summarize_trade_entry_outcomes(outcomes)
    pooled = summary[summary["scope"] == "pooled"]

    assert set(outcomes["anchor_event_id"]) == {"first-open", "second-open", "second-add"}
    assert list(pooled["horizon"]) == [1, 2, 3]
    assert list(pooled["n"]) == [3, 2, 1]


def test_lifecycle_summary_reports_pooled_and_per_symbol_equal_weights() -> None:
    outcomes = pd.DataFrame(
        [
            {"symbol": "A", "status": "complete", "mfe": 10.0, "mae": 5.0},
            {"symbol": "A", "status": "complete", "mfe": 20.0, "mae": 15.0},
            {"symbol": "B", "status": "complete", "mfe": 90.0, "mae": 25.0},
        ]
    )

    summary = summarize_trade_lifecycle_outcomes(outcomes)

    pooled = summary[summary["scope"] == "pooled"].iloc[0]
    symbol_a = summary[summary["symbol"] == "A"].iloc[0]
    assert pooled["n"] == 3
    assert pooled["median_mfe"] == 20.0
    assert symbol_a["n"] == 2
    assert symbol_a["median_mfe"] == 15.0


@pytest.mark.parametrize(
    ("events", "message"),
    [
        (
            [_event("close", 1, "close", remaining_quantity=0.0, pnl=0.0)],
            "requires an active position",
        ),
        (
            [_event("reduce", 1, "reduce", remaining_quantity=0.5, pnl=0.0)],
            "requires an active position",
        ),
        (
            [_event("open-1", 0, "open"), _event("open-2", 1, "open")],
            "requires a flat position",
        ),
        (
            [
                _event("open", 0, "open"),
                _event(
                    "close",
                    1,
                    "close",
                    side="short",
                    remaining_quantity=0.0,
                    pnl=0.0,
                ),
            ],
            "side cannot change",
        ),
        (
            [
                _event("open", 0, "open"),
                _event(
                    "add",
                    1,
                    "add",
                    price=120.0,
                    entry_price=105.0,
                    remaining_quantity=2.0,
                ),
            ],
            "weighted-average basis",
        ),
        (
            [
                _event("open", 0, "open"),
                _event(
                    "reduce",
                    1,
                    "reduce",
                    remaining_quantity=0.0,
                    pnl=0.0,
                ),
            ],
            "must leave a positive position",
        ),
        (
            [
                _event("open", 0, "open"),
                _event(
                    "close",
                    1,
                    "close",
                    fill_quantity=0.5,
                    remaining_quantity=0.0,
                    pnl=0.0,
                ),
            ],
            "must fully flatten",
        ),
        (
            [
                _event("same", 0, "open"),
                _event(
                    "same",
                    1,
                    "close",
                    remaining_quantity=0.0,
                    pnl=0.0,
                ),
            ],
            "event_id must be unique",
        ),
        (
            [
                _event("open", 2, "open"),
                _event(
                    "close",
                    1,
                    "close",
                    remaining_quantity=0.0,
                    pnl=0.0,
                ),
            ],
            "must not move backwards",
        ),
    ],
)
def test_malformed_lifecycle_events_fail(events: list[OrderEventRecord], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        compute_trade_lifecycle_outcomes(events, {"X": _ohlcv()})


@pytest.mark.parametrize(
    ("market_data", "error_type", "message"),
    [
        (_ohlcv(), TypeError, "mapping"),
        ({}, ValueError, "missing OHLCV"),
        ({"X": _ohlcv().tz_localize(None)}, ValueError, "timezone-aware"),
        ({"X": _ohlcv().iloc[::-1]}, ValueError, "sorted"),
        (
            {"X": pd.concat([_ohlcv(), _ohlcv().iloc[[-1]]])},
            ValueError,
            "unique",
        ),
    ],
)
def test_trade_market_data_contract_is_explicit(
    market_data: Any, error_type: type[Exception], message: str
) -> None:
    events = [
        _event("open", 0, "open"),
        _event("close", 2, "close", remaining_quantity=0.0, pnl=0.0),
    ]
    with pytest.raises(error_type, match=message):
        compute_trade_lifecycle_outcomes(events, market_data)


def test_naive_event_timestamp_fails() -> None:
    events = [
        _event("open", 0, "open"),
        _event("close", 2, "close", remaining_quantity=0.0, pnl=0.0),
    ]
    events[0] = replace(events[0], ts=events[0].ts.replace(tzinfo=None))

    with pytest.raises(ValueError, match="timestamp must be timezone-aware"):
        compute_trade_lifecycle_outcomes(events, {"X": _ohlcv()})


def test_invalid_trade_ohlcv_fails() -> None:
    frame = _ohlcv()
    frame.loc[frame.index[1], "high"] = np.inf
    events = [
        _event("open", 0, "open"),
        _event("close", 2, "close", remaining_quantity=0.0, pnl=0.0),
    ]

    with pytest.raises(ValueError, match="finite and positive"):
        compute_trade_lifecycle_outcomes(events, {"X": frame})


def test_same_timestamp_events_keep_input_sequence() -> None:
    events = [
        _event("open", 0, "open"),
        _event(
            "add",
            0,
            "add",
            price=120.0,
            entry_price=110.0,
            remaining_quantity=2.0,
        ),
        _event(
            "close",
            2,
            "close",
            fill_quantity=2.0,
            price=110.0,
            entry_price=110.0,
            remaining_quantity=0.0,
            pnl=0.0,
        ),
    ]

    outcome = compute_trade_lifecycle_outcomes(events, {"X": _ohlcv()}).iloc[0]

    assert outcome["status"] == "complete"
    assert outcome["mfe"] == 20.0


def test_trade_tearsheet_uses_lifecycle_and_anchor_populations(tmp_path: Path) -> None:
    events = [
        _event("open", 0, "open"),
        _event(
            "add",
            1,
            "add",
            price=110.0,
            entry_price=105.0,
            remaining_quantity=2.0,
        ),
        _event(
            "reduce",
            2,
            "reduce",
            price=115.0,
            entry_price=105.0,
            remaining_quantity=1.0,
            pnl=10.0,
        ),
        _event(
            "close",
            3,
            "close",
            price=100.0,
            entry_price=105.0,
            remaining_quantity=0.0,
            pnl=-5.0,
            periods_held=3,
        ),
    ]
    output = BacktestOutput(
        run_metadata=RunMetadata(
            run_id="test-20260101t0000-abcdef",
            strategy="test",
            symbols=("X",),
            timeframe="1h",
            data_source="fixture",
            started_at=T0,
            ended_at=T0 + timedelta(hours=7),
            run_at=T0 + timedelta(hours=8),
        ),
        equity_curve=(),
        order_events=events,
        metrics=StrategyMetrics(
            total_return=0.0,
            trades=2,
            win_rate=0.5,
            profit_factor=2.0,
        ),
        position_snapshots=(),
        allocation_snapshots=(),
    )
    output_path = tmp_path / "trade-tearsheet.html"

    result = generate_trade_tearsheet(
        output,
        {"X": _ohlcv()},
        output_path=str(output_path),
        max_periods=3,
    )
    html = output_path.read_text(encoding="utf-8")

    assert result == str(output_path)
    assert "Realized exits" in html
    assert "Completed lifecycles" in html
    assert "Entry anchors" in html
    assert "test · X · 1h" in html
