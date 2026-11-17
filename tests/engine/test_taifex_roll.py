"""A continuous series must not carry a position through a contract switch.

The bars' ``contract`` column says where the switch is; without it, a TAIFEX
continuous alias falls back to the venue's alias rule.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd
import pytest
from librae.backtest.engine import Backtest
from librae.config.symbols import get_symbol
from librae.core.cost_model import CostModel
from librae.core.run_config import AccountConfig, RunConfig
from librae.core.strategy import Context, OrderIntent, Strategy
from librae.core.taifex import final_settlement_follows, taifex_final_settlement_day
from librae.core.trading_calendar import period_start, session_bounds, session_label

pytest.importorskip("exchange_calendars")

CALENDAR = "XTAIFEX"
INITIAL_CASH = 10_000_000.0  # one TX contract at full margin
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


def _zero_costs(multiplier: float) -> CostModel:
    """No costs, but the contract's multiplier: a direct run books PnL with it."""
    return replace(CostModel.zero(), multiplier=multiplier)


def _registered_costs(frame: pd.DataFrame) -> CostModel:
    symbol = frame.index.get_level_values("symbol")[0]
    return _zero_costs(get_symbol(symbol).multiplier)


def _run(
    frame: pd.DataFrame,
    actions: dict[int, str | dict[str, object]],
    strategy: Strategy | None = None,
    data_source: str = "shioaji",  # the registered aliases' own source
) -> float:
    backtest = Backtest(
        frame,
        strategy or Scripted(actions),
        initial_balance=INITIAL_CASH,
        currency="TWD",
        cost_model=_registered_costs(frame),
        data_source=data_source,
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


class HoldUntilSettlementFollows(Strategy):
    """Hold the alias and exit on the flag, re-entering after the settlement session."""

    def __init__(self) -> None:
        self.flagged: list[date] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        session = session_label(ctx.ts, CALENDAR)
        if ctx.symbol in ctx.exit_before_roll:
            self.flagged.append(session)
            return [OrderIntent(action="close", symbol=ctx.symbol)] if ctx.positions else []
        if not ctx.positions and session != SETTLEMENT:
            return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
        return []


@pytest.mark.parametrize("stamps", [D1, _h1_stamps(DAYS)], ids=["D1", "H1"])
def test_a_strategy_exiting_on_the_flag_never_holds_through_settlement(
    stamps: pd.DatetimeIndex,
) -> None:
    strategy = HoldUntilSettlementFollows()
    Backtest(
        _frame("TXFR1", stamps),
        strategy,
        initial_balance=INITIAL_CASH,
        currency="TWD",
        cost_model=_zero_costs(200.0),
        data_source="shioaji",
    ).run()

    assert set(strategy.flagged) == {date(2026, 11, 17)}


@pytest.mark.parametrize(
    ("session", "expected"),
    [
        (date(2026, 11, 17), True),
        (date(2026, 11, 16), False),
        (SETTLEMENT, False),
        (date(2023, 1, 17), True),  # 01-18 was closed; settlement moved to 01-30
    ],
)
def test_final_settlement_follows(session: date, expected: bool) -> None:
    assert final_settlement_follows(session) is expected


# A series that moves to the next contract on the final settlement day, the
# night session before it included, so each bar is one contract's: here
# 2026-11-18 is already December's, priced 150 points below November.
SERIES_DAYS = [date(2026, 11, day) for day in (9, 10, 11, 12, 13, 16, 17, 18, 19, 20)]
SERIES_D1 = _d1_stamps(SERIES_DAYS)
ROLL_BAR = SERIES_DAYS.index(SETTLEMENT)
CONTRACTS = ["TX202611"] * ROLL_BAR + ["TX202612"] * (len(SERIES_DAYS) - ROLL_BAR)


def _series(symbol: str = "TXR1", contracts: list[object] | None = CONTRACTS) -> pd.DataFrame:
    frame = _frame(symbol, SERIES_D1)
    frame.iloc[ROLL_BAR:, :4] = 19_850.0
    if contracts is not None:
        frame["contract"] = pd.Series(contracts, index=frame.index, dtype=object)
    return frame


def _product_config(symbol: str = "TXR1", **route: object) -> RunConfig:
    """A series from the caller's own store, configured by its product alone."""
    return RunConfig(
        strategy_name="product-series",
        mode="backtest",
        symbols=(symbol,),
        timeframe="D1",
        market="tw_futures",
        data_source="my_store",
        account=AccountConfig(currency="TWD", initial_cash=INITIAL_CASH),
        instrument_overrides={
            symbol: {"product": "TX", "data_adapter": "test", "continuous_alias": True, **route}
        },
    )


def _run_configured(frame: pd.DataFrame, strategy: Strategy, config: RunConfig):
    return Backtest(frame, strategy, config=config, cost_model=CostModel.zero()).run()


class RollOnFlag(Strategy):
    """Stay long, exiting on the flag and re-entering on the next bar."""

    def __init__(self) -> None:
        self.flagged: list[date] = []

    def on_bar(self, ctx: Context) -> list[OrderIntent]:
        assert "contract" not in ctx.bar  # roll-guard input, not a market field
        if ctx.symbol in ctx.exit_before_roll:
            self.flagged.append(session_label(ctx.ts, CALENDAR))
            return [OrderIntent(action="close", symbol=ctx.symbol)] if ctx.positions else []
        if not ctx.positions:
            return [OrderIntent(action="long", symbol=ctx.symbol, quantity=1.0)]
        return []


def test_a_strategy_exiting_on_the_flag_rolls_without_booking_the_gap() -> None:
    strategy = RollOnFlag()

    result = _run_configured(_series(), strategy, _product_config())

    assert strategy.flagged == [date(2026, 11, 16)]
    events = [(e.ts, e.event_type, e.price) for e in result.position_events]
    assert events[:3] == [
        (SERIES_D1[1], "open", 20_000.0),
        (SERIES_D1[ROLL_BAR - 1], "close", 20_000.0),  # November's last bar
        (SERIES_D1[ROLL_BAR], "open", 19_850.0),  # December, held through its settlement day
    ]
    assert result.final_equity == pytest.approx(INITIAL_CASH)


@pytest.mark.parametrize("run", ["product_series", "registered_alias"])
def test_hold_into_the_next_contracts_bar_is_refused(run: str) -> None:
    pattern = (
        r"holds a position on contract 'TX202611' into the .* bar, which is priced on 'TX202612'"
    )
    with pytest.raises(ValueError, match=pattern) as err:
        if run == "product_series":
            _run_configured(_series(), Scripted({0: "long"}), _product_config())
        else:  # the bars' contracts win over the alias rule
            _run(_series("TXFR1"), {0: "long"})
    assert SERIES_D1[ROLL_BAR].isoformat() in str(err.value)


def test_exit_at_the_first_open_of_the_next_contract_is_refused() -> None:
    # The alias rule allows this fill; the bars say it is December's.
    actions: dict[int, str | dict[str, object]] = {0: "long", ROLL_BAR - 1: "close"}
    with pytest.raises(ValueError, match=r"into the .* bar, which is priced on 'TX202612'"):
        _run_configured(_series(), Scripted(actions), _product_config())


def test_exit_flag_follows_the_bars_not_the_alias_rule() -> None:
    strategy = RollOnFlag()

    _run(_series("TXFR1"), {}, strategy)

    # The alias rule flags 2026-11-17; November's last close is decided a bar earlier.
    assert strategy.flagged == [date(2026, 11, 16)]


def test_a_registered_alias_from_its_own_source_falls_back_to_the_alias_rule() -> None:
    strategy = RollOnFlag()
    unnamed = _series("TXFR1", contracts=[None] * len(SERIES_D1))

    _run(unnamed, {}, strategy)
    assert strategy.flagged == [date(2026, 11, 17)]
    with pytest.raises(ValueError, match="TAIFEX final settlement session"):
        _run(unnamed, {0: "long"})


REFUSED_WITHOUT_CONTRACTS = r"its bars do not name their contract.*add a 'contract' column"


def test_a_product_series_without_contracts_is_refused() -> None:
    with pytest.raises(ValueError, match=REFUSED_WITHOUT_CONTRACTS):
        _run_configured(_series(contracts=None), Scripted({}), _product_config())


@pytest.mark.parametrize("data_source", ["my_store", ""], ids=["other_source", "no_source"])
def test_a_registered_alias_read_from_elsewhere_without_contracts_is_refused(
    data_source: str,
) -> None:
    with pytest.raises(ValueError, match=REFUSED_WITHOUT_CONTRACTS):
        _run(_series("TMFR1", contracts=None), {}, data_source=data_source)


def test_a_rerouted_registered_alias_without_contracts_is_refused() -> None:
    config = RunConfig(
        strategy_name="rerouted-alias",
        mode="backtest",
        symbols=("TMFR1",),
        timeframe="D1",
        market="tw_futures",
        data_source="my_store",
        account=AccountConfig(currency="TWD", initial_cash=INITIAL_CASH),
        instrument_overrides={
            "TMFR1": {
                "data_source": "my_store",
                "data_adapter": "test",
                "instrument_type": "contract_monthly",
                "venue_symbol": "TMFR1",
            }
        },
    )
    with pytest.raises(ValueError, match=REFUSED_WITHOUT_CONTRACTS):
        _run_configured(_series("TMFR1", contracts=None), Scripted({}), config)


# A direct Backtest resolves every symbol as RunConfig does, so a continuous
# series under the caller's own name is guarded like a registered alias.
SERIES_OF_TX = {"product": "TX", "continuous_alias": True}
DIRECT_SERIES = [("TX_CONT", "shioaji"), ("TXR1", "finmind"), ("TXR1", "")]


def _run_direct(
    frame: pd.DataFrame,
    strategy: Strategy,
    data_source: str,
    instrument_overrides: dict[str, dict[str, object]] | None = None,
    cost_model: CostModel | None = None,
):
    return Backtest(
        frame,
        strategy,
        initial_balance=INITIAL_CASH,
        currency="TWD",
        cost_model=cost_model or _zero_costs(200.0),
        data_source=data_source,
        instrument_overrides=instrument_overrides,
    ).run()


@pytest.mark.parametrize(("symbol", "data_source"), DIRECT_SERIES)
def test_a_direct_series_neither_registered_nor_declared_is_refused(
    symbol: str, data_source: str
) -> None:
    pattern = f"'{symbol}' is neither a registered symbol nor declared in instrument_overrides"
    with pytest.raises(ValueError, match=pattern):
        _run_direct(_series(symbol), Scripted({0: "long"}), data_source)


@pytest.mark.parametrize(("symbol", "data_source"), DIRECT_SERIES)
def test_a_declared_direct_series_is_guarded_by_its_contracts(
    symbol: str, data_source: str
) -> None:
    declared = {symbol: SERIES_OF_TX}
    with pytest.raises(ValueError, match=r"into the .* bar, which is priced on 'TX202612'"):
        _run_direct(_series(symbol), Scripted({0: "long"}), data_source, declared)

    strategy = RollOnFlag()
    result = _run_direct(_series(symbol), strategy, data_source, declared)
    assert strategy.flagged == [date(2026, 11, 16)]
    assert result.final_equity == pytest.approx(INITIAL_CASH)


@pytest.mark.parametrize(("symbol", "data_source"), DIRECT_SERIES)
def test_a_declared_direct_series_without_contracts_is_refused(
    symbol: str, data_source: str
) -> None:
    with pytest.raises(ValueError, match=REFUSED_WITHOUT_CONTRACTS):
        _run_direct(
            _series(symbol, contracts=None), Scripted({}), data_source, {symbol: SERIES_OF_TX}
        )


def test_a_declared_direct_series_books_its_products_multiplier() -> None:
    frame = _series(contracts=["TX202611"] * len(SERIES_D1))
    frame.iloc[2:, :4] = 20_300.0  # +300 points after the entry fill at bar 1's open

    result = _run_direct(frame, Scripted({0: "long"}), "finmind", {"TXR1": SERIES_OF_TX})

    assert result.final_equity - INITIAL_CASH == pytest.approx(300.0 * 200.0)


def test_a_direct_cost_model_contradicting_the_products_multiplier_is_refused() -> None:
    with pytest.raises(ValueError, match=r"'TXR1' has multiplier=200.0 .* multiplier=200.0"):
        _run_direct(_series(), Scripted({}), "finmind", {"TXR1": SERIES_OF_TX}, CostModel.zero())


def test_a_direct_product_declaration_must_select_a_contract() -> None:
    with pytest.raises(ValueError, match="exactly one of continuous_alias=True"):
        _run_direct(_series(), Scripted({}), "finmind", {"TXR1": {"product": "TX"}})


def test_contracts_on_some_bars_only_are_refused() -> None:
    contracts: list[object] = list(CONTRACTS)
    contracts[1] = None

    with pytest.raises(ValueError, match="names a 'contract' on some bars only"):
        _run(_series("TXFR1", contracts), {})
