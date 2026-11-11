"""A TAIFEX continuous alias must not carry a position through a contract switch."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.core.cost_model import CostModel
from librae.core.run_config import AccountConfig, RunConfig
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.core.taifex import taifex_final_settlement_day
from librae.core.trading_calendar import period_start, session_bounds, session_label

pytest.importorskip("exchange_calendars")

CALENDAR = "XTAIFEX"
INITIAL_CASH = 1_000_000.0
SETTLEMENT = date(2026, 11, 18)  # the 202611 contracts' final settlement day


@pytest.mark.parametrize(
    ("contract_month", "expected"),
    [
        # https://www.taifex.com.tw/cht/5/futIndxFSP, read 2026-11-11
        ("202201", date(2022, 1, 19)),
        ("202301", date(2023, 1, 30)),  # closed on the third Wednesday, 01-18
        ("202402", date(2024, 2, 21)),
        ("202410", date(2024, 10, 16)),
        ("202602", date(2026, 2, 23)),  # third Wednesday 02-18 falls in the new-year closure
        ("202610", date(2026, 10, 21)),
    ],
)
def test_final_settlement_day_matches_taifex(contract_month: str, expected: date) -> None:
    assert taifex_final_settlement_day(contract_month) == expected


@pytest.mark.parametrize("contract_month", ["2026-11", "202613", 202611])
def test_final_settlement_day_rejects_a_malformed_month(contract_month: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        taifex_final_settlement_day(contract_month)  # type: ignore[arg-type]


def _d1_stamps(days: list[date]) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([session_bounds(day, CALENDAR)[0] for day in days])


def _h1_stamps(days: list[date]) -> pd.DatetimeIndex:
    stamps: set[pd.Timestamp] = set()
    for day in days:
        first_open, final_close = session_bounds(day, CALENDAR)
        for ts in pd.date_range(first_open, final_close, freq="15min", inclusive="left"):
            try:
                if session_label(ts, CALENDAR) == day:
                    stamps.add(period_start(ts, "H1", CALENDAR))
            except ValueError:  # between the night and day segments
                continue
    return pd.DatetimeIndex(sorted(stamps))


DAYS = [
    date(2026, 11, 16),
    date(2026, 11, 17),
    SETTLEMENT,
    date(2026, 11, 19),
    date(2026, 11, 20),
    date(2026, 11, 23),
]
D1 = _d1_stamps(DAYS)
SETTLEMENT_BAR = DAYS.index(SETTLEMENT)


def _frame(symbol: str, stamps: pd.DatetimeIndex) -> pd.DataFrame:
    index = pd.MultiIndex.from_arrays(
        [[symbol] * len(stamps), stamps], names=["symbol", "datetime"]
    )
    return pd.DataFrame(
        {"open": 20_000.0, "high": 20_000.0, "low": 20_000.0, "close": 20_000.0, "volume": 100.0},
        index=index,
    )


class Scripted(Strategy):
    """Decide ``actions[period_index]`` at that bar's close; it fills at the next open."""

    def __init__(self, actions: dict[int, str | dict[str, object]]) -> None:
        self.actions = actions

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        action = self.actions.get(ctx.period_index)
        if action is None:
            return []
        fields = {"action": action} if isinstance(action, str) else dict(action)
        if fields["action"] == "long":
            fields.setdefault("quantity", 1.0)
        return [OrderIntent(symbol=ctx.symbol, **fields)]  # type: ignore[arg-type]


def _run(frame: pd.DataFrame, actions: dict[int, str | dict[str, object]]) -> float:
    backtest = Backtest(
        frame,
        Scripted(actions),
        initial_balance=INITIAL_CASH,
        cost_model=CostModel.zero(),
        data_source="test",
    )
    return backtest.run().final_equity


@pytest.mark.parametrize("symbol", ["TXFR1", "TXFR2", "MXFR1", "TMFR2"])
def test_hold_through_the_settlement_session_is_refused(symbol: str) -> None:
    with pytest.raises(ValueError, match=f"'{symbol}' holds a position at the close") as err:
        _run(_frame(symbol, D1), {0: "long"})
    assert D1[SETTLEMENT_BAR].isoformat() in str(err.value)
    assert "2026-11-18 TAIFEX final settlement session" in str(err.value)


def test_exit_at_the_settlement_session_open_is_allowed() -> None:
    actions = {0: "long", SETTLEMENT_BAR - 1: "close", SETTLEMENT_BAR: "long"}

    assert _run(_frame("TXFR1", D1), actions) == pytest.approx(INITIAL_CASH)


def test_entry_at_the_settlement_session_open_is_refused() -> None:
    with pytest.raises(ValueError, match="holds a position at the close"):
        _run(_frame("TXFR1", D1), {SETTLEMENT_BAR - 1: "long"})


def test_hold_over_a_settlement_session_without_a_bar_is_refused() -> None:
    gapped = _frame("TXFR1", D1.delete(SETTLEMENT_BAR))

    # The exit fills at the next bar's open, already on the new contract.
    with pytest.raises(ValueError, match="through the 2026-11-18 TAIFEX final settlement"):
        _run(gapped, {0: "long", SETTLEMENT_BAR - 1: "close"})


def _first_bar_of(
    stamps: pd.DatetimeIndex, after: date | None = None, on: date | None = None
) -> int:
    for i, ts in enumerate(stamps):
        label = session_label(ts, CALENDAR)
        if (on is not None and label == on) or (after is not None and label > after):
            return i
    raise AssertionError("no such bar")


def test_intraday_hold_into_the_settlement_session_is_refused() -> None:
    stamps = _h1_stamps(DAYS[1:4])
    first_settlement_bar = _first_bar_of(stamps, on=SETTLEMENT)

    with pytest.raises(ValueError, match="2026-11-18 TAIFEX final settlement session") as err:
        _run(_frame("TXFR1", stamps), {0: "long"})
    assert stamps[first_settlement_bar].isoformat() in str(err.value)


def test_intraday_exit_at_the_settlement_session_open_is_allowed() -> None:
    stamps = _h1_stamps(DAYS[1:4])
    first_settlement_bar = _first_bar_of(stamps, on=SETTLEMENT)
    first_next_bar = _first_bar_of(stamps, after=SETTLEMENT)
    actions = {0: "long", first_settlement_bar - 1: "close", first_next_bar - 1: "long"}

    assert _run(_frame("TXFR1", stamps), actions) == pytest.approx(INITIAL_CASH)


def test_exact_contract_hold_is_unaffected() -> None:
    symbol = "TXF_202612"
    config = RunConfig(
        strategy_name="exact-contract",
        mode="backtest",
        symbols=(symbol,),
        timeframe="D1",
        market="tw_futures",
        data_source="test",
        account=AccountConfig(currency="TWD", initial_cash=INITIAL_CASH),
        symbol_cost_overrides={symbol: {"multiplier": 1.0, "commission_per_contract": 0.0}},
        instrument_overrides={
            symbol: {
                "data_adapter": "test",
                "instrument_type": "contract_monthly",
                "contract_month": "202612",
                "currency": "TWD",
                "calendar_id": CALENDAR,
            }
        },
    )

    result = Backtest(
        _frame(symbol, D1),
        Scripted({0: "long"}),
        config=config,
        cost_model=CostModel.zero(),
    ).run()

    assert result.final_equity == pytest.approx(INITIAL_CASH)
    assert result.position_events


def _settlement_bar_trades_both_contracts() -> pd.DataFrame:
    """Old contract at the open, the next one 150 points lower by the close."""
    frame = _frame("TXFR1", D1)
    frame.iloc[SETTLEMENT_BAR] = [20_000.0, 20_150.0, 19_850.0, 19_850.0, 100.0]
    frame.iloc[SETTLEMENT_BAR + 1 :, :4] = 19_850.0
    return frame


def test_stop_filled_inside_the_settlement_bar_is_refused() -> None:
    actions: dict[int, str | dict[str, object]] = {0: {"action": "long", "stop_price": 19_900.0}}

    with pytest.raises(ValueError, match=r"'TXFR1' fills a \w+ at 19900\.0 inside the") as err:
        _run(_settlement_bar_trades_both_contracts(), actions)
    assert "2026-11-18 TAIFEX final settlement session" in str(err.value)


def test_limit_exit_filled_inside_the_settlement_bar_is_refused() -> None:
    actions: dict[int, str | dict[str, object]] = {
        0: "long",
        SETTLEMENT_BAR - 1: {"action": "close", "limit_price": 20_100.0},
    }

    with pytest.raises(ValueError, match=r"fills a \w+ at 20100\.0 inside the"):
        _run(_settlement_bar_trades_both_contracts(), actions)


def test_limit_exit_marketable_at_the_settlement_open_is_allowed() -> None:
    actions: dict[int, str | dict[str, object]] = {
        0: "long",
        SETTLEMENT_BAR - 1: {"action": "close", "limit_price": 19_950.0},
    }

    assert _run(_settlement_bar_trades_both_contracts(), actions) == pytest.approx(INITIAL_CASH)
