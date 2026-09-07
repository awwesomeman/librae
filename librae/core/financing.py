"""Deterministic perpetual-funding and short-borrow cash-flow accounting.

Funding and borrow share a record, a table and an accrual pass, which makes
it tempting to give a new borrow feature the shape of the funding one beside
it. Their premises differ at almost every point, and three bugs have come
from copying across that line -- one shipped, two caught in review:

===================  ==============================  ==========================
                     funding                         borrow
===================  ==============================  ==========================
what it is           a discrete settlement            a rate that stays in
                                                      force until repriced
who pays             both sides, sign follows the     the short only
                     position
joined onto bars     nearest match, tight tolerance   backward as-of, bounded
                     (the payment owns one bar)       carry (see
                                                      attach_borrow_rate)
quoted per           contract                         borrowed currency
endpoint             public                           signed -- needs
                                                      credentials, and its
                                                      limit is not a bar count
===================  ==============================  ==========================

Before extending either one, check which column the new code belongs in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isclose, isfinite, isnan
from typing import Literal

import pandas as pd

from librae.core import EPSILON
from librae.core.cost_model import CostModel
from librae.core.executor import side_multiplier
from librae.core.strategy import PositionEventType, PositionSide, PositionState
from librae.core.utils import interval_to_timedelta

FUNDING_RATE_FIELD = "funding_rate"
FUNDING_MARK_PRICE_FIELD = "funding_mark_price"
BORROW_RATE_FIELD = "borrow_rate"

FinancingKind = Literal["funding", "borrow"]


@dataclass(frozen=True, slots=True)
class FinancingLifecycleEvent:
    """Quantity-changing position event used for financing attribution."""

    ts: datetime
    symbol: str
    entry_at: datetime
    event_type: PositionEventType
    fill_quantity: float
    remaining_quantity: float


@dataclass(slots=True)
class _FinancingBalance:
    quantity: float
    accrued: float = 0.0


# How many *quoting* periods a borrow rate keeps describing the market. A rate
# is a step function, so it must carry forward past the bar it was published
# on -- but not indefinitely.
#
# The quoting period is what a venue reports (Binance quotes a daily rate, so
# 1 day); how often it republishes is not, and is far shorter -- observed every
# 1-20h on Binance. So this bound is generous by roughly an order of magnitude,
# deliberately: erring long keeps charging a slightly stale rate, while erring
# short stops charging at all, and a cost model should fail toward
# overcharging.
BORROW_RATE_MAX_AGE_QUOTING_PERIODS = 3


@dataclass(frozen=True, slots=True)
class FinancingCashFlow:
    """One financing payment calculated from a confirmed open position.

    ``kind`` separates the two: ``funding`` is a perpetual's periodic
    settlement, whose sign flips with the position side; ``borrow`` is
    interest on the asset a short had to borrow, which only the short side
    ever pays. Both are per-bar cash flows attributed to one trade, so they
    share this record and their storage.

    group_id/entry_at are copied from the accruing PositionState so this
    payment can be attributed back to the trade it belongs to (see
    librae.core.metrics._collapse_trades_by_group's (group_id, entry_at) key)
    — entry_at is stable across a position's whole hold (only set on open,
    untouched by scale-ins).
    """

    ts: datetime
    symbol: str
    side: PositionSide
    quantity: float
    mark_price: float
    multiplier: float
    rate: float
    cash_flow: float
    group_id: str | None
    entry_at: datetime
    kind: FinancingKind = "funding"


def attribute_financing_to_closes(
    position_events: Sequence[FinancingLifecycleEvent],
    cash_flows: Sequence[FinancingCashFlow],
) -> list[float]:
    """Allocate accrued financing to closes in position-event order.

    Financing remains attached to the quantity that is open when it accrues.
    A partial close releases the same fraction of the position's accumulated
    balance that the fill removes, matching Librae's average-cost position
    accounting. Position events precede financing at an equal timestamp,
    which mirrors the engine's execution-then-accrual cycle.
    """
    close_event_indexes = [
        index
        for index, event in enumerate(position_events)
        if event.event_type in ("reduce", "close")
    ]
    close_output_indexes = {
        event_index: output_index for output_index, event_index in enumerate(close_event_indexes)
    }
    attributed = [0.0] * len(close_event_indexes)
    balances: dict[tuple[str, datetime], _FinancingBalance] = {}

    timeline = [(event.ts, 0, index, event) for index, event in enumerate(position_events)]
    timeline.extend(
        (cash_flow.ts, 1, index, cash_flow) for index, cash_flow in enumerate(cash_flows)
    )
    timeline.sort(key=lambda item: (item[0], item[1], item[2]))

    for _, item_type, item_index, item in timeline:
        key = (item.symbol, item.entry_at)
        if item_type == 1:
            cash_flow = item
            if cash_flow.quantity <= EPSILON:
                raise ValueError("financing cash flow quantity must be positive")
            balance = balances.get(key)
            if balance is None:
                balance = _FinancingBalance(quantity=cash_flow.quantity)
                balances[key] = balance
            elif not isclose(
                balance.quantity,
                cash_flow.quantity,
                rel_tol=1e-9,
                abs_tol=EPSILON,
            ):
                raise ValueError("financing cash flow quantity does not match lifecycle state")
            balance.accrued += cash_flow.cash_flow
            continue

        event = item
        if min(event.fill_quantity, event.remaining_quantity) < 0:
            raise ValueError("financing lifecycle quantities must be non-negative")
        balance = balances.get(key)
        if event.event_type in ("open", "add"):
            prior_quantity = event.remaining_quantity - event.fill_quantity
            if event.event_type == "open":
                prior_quantity = 0.0
            if balance is not None and not isclose(
                balance.quantity,
                prior_quantity,
                rel_tol=1e-9,
                abs_tol=EPSILON,
            ):
                raise ValueError("position entry quantity does not match lifecycle state")
            if balance is None:
                balance = _FinancingBalance(quantity=prior_quantity)
                balances[key] = balance
            balance.quantity = event.remaining_quantity
            continue

        if event.event_type not in ("reduce", "close"):
            continue
        quantity_before_close = event.fill_quantity + event.remaining_quantity
        if quantity_before_close <= EPSILON:
            raise ValueError("position close must have positive pre-close quantity")
        if balance is None:
            balance = _FinancingBalance(quantity=quantity_before_close)
            balances[key] = balance
        elif not isclose(
            balance.quantity,
            quantity_before_close,
            rel_tol=1e-9,
            abs_tol=EPSILON,
        ):
            raise ValueError("position close quantity does not match lifecycle state")

        if event.remaining_quantity <= EPSILON:
            released = balance.accrued
            balances.pop(key)
        else:
            released = balance.accrued * event.fill_quantity / quantity_before_close
            balance.accrued -= released
            balance.quantity = event.remaining_quantity
        attributed[close_output_indexes[item_index]] = released

    return attributed


def _optional_finite_float(value: object, *, field: str, symbol: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{symbol} {field} must be numeric or missing")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{symbol} {field} must be numeric or missing") from exc
    if isnan(number):
        return None
    if not isfinite(number):
        raise ValueError(f"{symbol} {field} must be finite")
    return number


def _mark_price(bar: Mapping[str, object], symbol: str, *, override_field: str | None) -> float:
    """Resolve the price this accrual is valued at, preferring an override."""
    price = None
    if override_field is not None:
        price = _optional_finite_float(bar.get(override_field), field=override_field, symbol=symbol)
    if price is None:
        price = _optional_finite_float(bar.get("close"), field="close", symbol=symbol)
    if price is None or price <= 0:
        raise ValueError(f"{symbol} financing mark price must be finite and positive")
    return price


def _accrue(
    ts: datetime,
    bars: Mapping[str, Mapping[str, object]],
    positions: Mapping[str, PositionState],
    *,
    get_cost_model: Callable[[str], CostModel],
    rate_field: str,
    kind: FinancingKind,
    mark_price_field: str | None,
) -> tuple[tuple[str, ...], list[FinancingCashFlow]]:
    observed_symbols: list[str] = []
    cash_flows: list[FinancingCashFlow] = []
    for symbol in sorted(bars):
        bar = bars[symbol]
        if rate_field not in bar:
            continue
        rate = _optional_finite_float(bar[rate_field], field=rate_field, symbol=symbol)
        if rate is None:
            continue
        observed_symbols.append(symbol)

        mark_price = _mark_price(bar, symbol, override_field=mark_price_field)
        position = positions.get(symbol)
        if position is None:
            continue
        if kind == "borrow" and position.side != "short":
            continue

        cost_model = get_cost_model(symbol)
        notional = position.quantity * mark_price * cost_model.multiplier
        # Funding flips with the side; borrow is a cost the short always pays.
        signed_rate = side_multiplier(position.side) * rate if kind == "funding" else rate
        cash_flow = -signed_rate * notional
        if not isfinite(notional) or not isfinite(cash_flow):
            raise ValueError(f"{symbol} {kind} cash flow must be finite")
        cash_flows.append(
            FinancingCashFlow(
                ts=ts,
                symbol=symbol,
                side=position.side,
                quantity=float(position.quantity),
                mark_price=mark_price,
                multiplier=float(cost_model.multiplier),
                rate=rate,
                cash_flow=float(cash_flow),
                group_id=position.group_id,
                entry_at=position.entry_at,
                kind=kind,
            )
        )
    return tuple(observed_symbols), cash_flows


def calculate_funding_cash_flows(
    ts: datetime,
    bars: Mapping[str, Mapping[str, object]],
    positions: Mapping[str, PositionState],
    *,
    get_cost_model: Callable[[str], CostModel],
) -> tuple[tuple[str, ...], list[FinancingCashFlow]]:
    """Calculate funding payments present on one market-data event.

    ``funding_rate`` is a decimal rate per payment. A positive rate means
    longs pay shorts. ``funding_mark_price`` is optional and otherwise falls
    back to the same event's raw close. Missing rates are not forward-filled.

    Returns the symbols whose observations were consumed and the payments for
    symbols with confirmed open positions.
    """
    return _accrue(
        ts,
        bars,
        positions,
        get_cost_model=get_cost_model,
        rate_field=FUNDING_RATE_FIELD,
        kind="funding",
        mark_price_field=FUNDING_MARK_PRICE_FIELD,
    )


def calculate_borrow_cash_flows(
    ts: datetime,
    bars: Mapping[str, Mapping[str, object]],
    positions: Mapping[str, PositionState],
    *,
    get_cost_model: Callable[[str], CostModel],
) -> tuple[tuple[str, ...], list[FinancingCashFlow]]:
    """Calculate short-borrow interest present on one market-data event.

    ``borrow_rate`` is a decimal rate for *this* accrual event, already
    matched to the bar interval by whoever supplied it — the engine applies
    it as given and never annualizes, exactly as it treats ``funding_rate``.

    Only shorts accrue: borrowing an asset to sell it costs interest, and
    the long side receives nothing in return, so unlike funding the sign
    never flips. Missing rates are not forward-filled — an absent rate means
    "unknown", which must not be read as "free".

    A perpetual's holding cost is already expressed by funding, so a
    perpetual bar must not carry ``borrow_rate`` or the position pays twice.
    Whoever binds the market data decides which instruments borrow.
    """
    return _accrue(
        ts,
        bars,
        positions,
        get_cost_model=get_cost_model,
        rate_field=BORROW_RATE_FIELD,
        kind="borrow",
        mark_price_field=None,
    )


def attach_borrow_rate(
    bars: pd.DataFrame,
    rates: pd.DataFrame,
    *,
    timeframe: str,
) -> pd.DataFrame:
    """Turn a venue's borrow-rate series into a per-bar ``borrow_rate`` column.

    Use this wherever bars are assembled -- a caller's backtest data layer as
    much as the live market-data binding -- so both charge the same position
    the same interest. Three rules have to agree for that to hold, and each is
    easy to get wrong on its own:

    - **Scaling.** A venue quotes a rate over its own period (Binance: daily)
      while the engine charges once per bar, so the rate is scaled by the bar
      interval. Hardcoding the ratio is correct only for the bar size it was
      written for.
    - **Direction.** A borrow rate is a step function, not a settlement: the
      last published rate stays in force until the next one, so it is joined
      backward as-of and every later bar accrues at it. Matching the nearest
      rate instead would charge one bar per publication and leave the rest
      free.
    - **Staleness.** The carry stops after
      ``BORROW_RATE_MAX_AGE_QUOTING_PERIODS`` quoting periods, leaving the
      column NaN so the engine reads "unknown" rather than "free". An
      unbounded carry keeps charging a rate the venue stopped standing behind.

    Args:
        bars: Bars with a ``ts`` column, ascending.
        rates: ``[ts, borrow_rate, rate_period_seconds]`` as returned by an
            adapter's ``fetch_borrow_rate_history``. ``rate_period_seconds``
            is the quoting basis, not the republication cadence.
        timeframe: The bars' interval, in either ccxt or canonical form.

    Returns ``bars`` with a ``borrow_rate`` column, NaN where no rate applies.
    Returns it unchanged when ``rates`` is empty -- absent means unknown, and
    the engine skips a missing rate rather than treating it as zero.
    """
    if bars.empty or rates.empty:
        return bars

    period = rates["rate_period_seconds"].astype(float)
    bar_seconds = interval_to_timedelta(timeframe).total_seconds()
    scaled = rates.assign(
        borrow_rate=rates[BORROW_RATE_FIELD].astype(float) * bar_seconds / period
    ).sort_values("ts")
    max_age = pd.Timedelta(seconds=float(period.max()) * BORROW_RATE_MAX_AGE_QUOTING_PERIODS)
    return pd.merge_asof(
        bars,
        scaled[["ts", BORROW_RATE_FIELD]],
        on="ts",
        direction="backward",
        tolerance=max_age,
    )
