"""The console's SQL and data shaping, without a database or a browser."""

from __future__ import annotations

import pandas as pd
import pytest
from librae.console import queries

from tests.signal_outcome_contract import (
    SIGNAL_OUTCOME_LONG_FRACTIONS,
    make_signal_outcome_contract_ohlcv,
)
from tests.sql_schema_check import columns_by_table, single_table, unknown_columns

NOW = pd.Timestamp("2026-03-01 12:00", tz="UTC")
STATEMENTS = {
    name: value
    for name, value in vars(queries).items()
    if name.isupper() and isinstance(value, str) and "SELECT" in value
}


def test_single_table_statements_name_columns_that_exist() -> None:
    """The suite never executes this SQL, so a renamed column would surface
    only as an error on the page."""
    schema = columns_by_table()
    checked = {name: single_table(sql, schema) for name, sql in STATEMENTS.items()}
    unknown = {
        name: unknown_columns(*resolved, schema)
        for name, resolved in checked.items()
        if resolved is not None and unknown_columns(*resolved, schema)
    }

    assert unknown == {}
    assert sum(resolved is not None for resolved in checked.values()) >= 10


def test_statements_only_read() -> None:
    for name, sql in STATEMENTS.items():
        words = set(sql.upper().split())
        assert not words & {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}, name


@pytest.mark.parametrize(
    ("heartbeat", "poll", "expected"),
    [
        (NOW - pd.Timedelta(seconds=170), 60, "running"),
        (NOW - pd.Timedelta(seconds=181), 60, "stale"),
        (None, 60, "not polling"),
        (NOW, None, "not polling"),
        (NOW, 0, "not polling"),
    ],
)
def test_run_status_allows_three_missed_polls(heartbeat, poll, expected) -> None:
    assert queries.run_status(heartbeat, poll, NOW) == expected


def test_with_status_flags_halted_and_behind_runs_only_while_polling() -> None:
    frame = pd.DataFrame(
        {
            "last_heartbeat_at": [NOW, NOW, pd.NaT],
            "poll_seconds": [60.0, 60.0, float("nan")],
            "halted": [True, None, True],
            "last_equity_at": [NOW, NOW - pd.Timedelta(minutes=14), NOW - pd.Timedelta(days=9)],
            "timeframe": ["M5", "M5", "M5"],
        }
    )

    out = queries.with_status(frame, NOW)

    assert out["status"].tolist() == ["halted", "running", "not polling"]
    assert out["behind"].tolist() == [False, True, False]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (pd.Timedelta(minutes=12), False),  # two M5 bars plus three 60 s polls is 13 min
        (pd.Timedelta(minutes=13, seconds=1), True),
    ],
)
def test_is_behind_allows_two_bars_and_three_polls(age, expected) -> None:
    assert queries.is_behind(NOW - age, "M5", 60, NOW) is expected


def test_is_behind_without_data_or_poll_interval() -> None:
    assert queries.is_behind(None, "D1", 60, NOW) is False
    assert queries.is_behind(NOW - pd.Timedelta(days=2, seconds=1), "D1", None, NOW) is True


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(45, "45 s"), (600, "10 min"), (5400, "90 min"), (3 * 3600, "3.0 h"), (5 * 86400, "5 d")],
)
def test_format_age(seconds, text) -> None:
    assert queries.format_age(pd.Timedelta(seconds=seconds)) == text


def test_drawdown_is_measured_from_the_running_peak() -> None:
    equity = pd.DataFrame({"equity": [100.0, 120.0, 90.0, 130.0]})

    assert queries.with_drawdown(equity)["drawdown"].tolist() == pytest.approx(
        [0.0, 0.0, -0.25, 0.0]
    )


def test_position_snapshot_marks_to_the_latest_bar_with_the_multiplier() -> None:
    positions = pd.DataFrame(
        [
            {
                "symbol": "FUT",
                "side": "short",
                "remaining_quantity": 2.0,
                "entry_price": 100.0,
                "price": 100.0,
                "fill_quantity": 2.0,
                "notional": 10_000.0,  # multiplier 50
                "margin_locked": 1_000.0,
                "leverage": 10.0,
                "margin_mode": "fixed",
                "liquidation_price": 120.0,
                "entry_at": NOW,
            },
            {
                "symbol": "NOBAR",
                "side": "long",
                "remaining_quantity": 1.0,
                "entry_price": 10.0,
                "price": 10.0,
                "fill_quantity": 1.0,
                "notional": 10.0,
                "margin_locked": 10.0,
                "leverage": 1.0,
                "margin_mode": "unlevered",
                "liquidation_price": None,
                "entry_at": NOW,
            },
        ]
    )
    marks = pd.DataFrame({"symbol": ["FUT"], "ts": [NOW], "close": [90.0]})

    snapshot = queries.position_snapshot(positions, marks, equity=20_000.0)

    fut = snapshot.iloc[0]
    assert fut["symbol"] == "FUT"
    assert fut["mtm_pnl"] == pytest.approx(1_000.0)  # (100 - 90) * 2 * 50
    assert fut["mtm_return"] == pytest.approx(0.10)
    assert fut["weight"] == pytest.approx(-0.45)  # -(2 * 90 * 50) / 20_000
    assert fut["liquidation_buffer"] == pytest.approx(30 / 90)
    unmarked = snapshot.iloc[1]
    assert pd.isna(unmarked["market_price"]) and pd.isna(unmarked["mtm_pnl"])


def test_trade_returns_count_hedge_legs_as_one_trade() -> None:
    entry = NOW - pd.Timedelta(hours=1)
    events = pd.DataFrame(
        {
            "net_return": [None, 5.0, 10.0, -6.0],  # percentage points; None is an entry
            "group_id": [None, None, "hedge", "hedge"],
            "entry_at": [pd.NaT, entry, entry, entry],
            "realized_pnl": [None, 5.0, 10.0, -6.0],
            "notional": [100.0, 100.0, 100.0, 100.0],
            "entry_price": [10.0, 10.0, 10.0, 10.0],
            "price": [10.0, 10.0, 10.0, 10.0],
        }
    )

    # The ungrouped exit keeps its 5%; the two legs net 4 on 200 of notional.
    assert sorted(queries.trade_returns(events)) == pytest.approx([0.02, 0.05])


def test_oldest_first_restores_time_order() -> None:
    newest_first = pd.DataFrame({"ts": [3, 2, 1]})

    assert queries.oldest_first(newest_first)["ts"].tolist() == [1, 2, 3]


def test_fill_markers_label_direction_and_side() -> None:
    events = pd.DataFrame(
        {
            "ts": [NOW, NOW],
            "price": [1.0, 2.0],
            "event_type": ["add", "close"],
            "side": ["long", "short"],
        }
    )

    assert queries.fill_markers(events)["label"].tolist() == ["Entry long", "Exit short"]


def test_signal_markers_sit_on_the_latest_bar_at_or_before_the_signal() -> None:
    bars = pd.DataFrame(
        {"ts": pd.date_range("2026-03-01", periods=3, freq="1h", tz="UTC"), "close": [1, 2, 3]}
    )
    signals = pd.DataFrame({"ts": [bars["ts"][1] + pd.Timedelta(minutes=30)]})

    assert queries.signal_markers(signals, bars, "Entry")["price"].tolist() == [2]


def _contract_bars() -> pd.DataFrame:
    return make_signal_outcome_contract_ohlcv().rename_axis("ts").reset_index()


def test_signal_outcomes_follow_the_golden_contract_in_fractions() -> None:
    bars = _contract_bars()
    signal = pd.Series([bars["ts"][0]])

    for horizon in (1, 2, 3):
        outcome = queries.signal_outcomes_at(signal, bars, horizon, "long", "open").iloc[0]
        for column, expected in SIGNAL_OUTCOME_LONG_FRACTIONS.items():
            assert outcome[column] == pytest.approx(expected[horizon - 1]), (column, horizon)


def test_signal_outcomes_leave_out_signals_older_than_the_loaded_bars() -> None:
    bars = _contract_bars()
    before = pd.Series([bars["ts"][0] - pd.Timedelta(hours=5)])

    assert queries.signal_outcomes_at(before, bars, 1, "long", "open").empty


def test_signal_outcomes_leave_out_signals_without_enough_bars() -> None:
    bars = _contract_bars()

    assert queries.signal_outcomes_at(pd.Series([bars["ts"][0]]), bars, 5, "long", "open").empty


def test_rolling_stats_and_edge_ratio() -> None:
    outcomes = pd.DataFrame(
        {
            "ts": pd.date_range("2026-03-01", periods=3, freq="1h", tz="UTC"),
            "forward_return": [0.01, -0.02, 0.03],
            "mfe": [0.02, 0.01, 0.03],
            "mae": [0.01, 0.03, 0.0],
        }
    )

    stats = queries.rolling_signal_stats(outcomes, window=2)

    assert stats["cumulative_return"].tolist() == pytest.approx([0.01, -0.01, 0.02])
    assert stats["rolling_mean_return"].tolist()[1:] == pytest.approx([-0.005, 0.005])
    assert stats["rolling_edge_ratio"].tolist()[1:] == pytest.approx([0.75, 4 / 3])
    assert queries.edge_ratio(outcomes) == pytest.approx(0.06 / 0.04)


def test_latest_signal_return_is_direction_adjusted() -> None:
    bars = _contract_bars()  # next bar opens at 100, last close is 100
    bars.loc[bars.index[-1], "close"] = 110.0
    signal = pd.Series([bars["ts"][0]])

    assert queries.latest_signal_return(signal, bars, "long", "open") == pytest.approx(0.10)
    assert queries.latest_signal_return(signal, bars, "short", "open") == pytest.approx(-0.10)
    assert (
        queries.latest_signal_return(pd.Series([bars["ts"].iloc[-1]]), bars, "long", "open") is None
    )
