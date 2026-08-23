"""Deterministic perpetual-funding and short-borrow cash-flow accounting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, isnan
from typing import Literal

from librae.core.cost_model import CostModel
from librae.core.executor import side_multiplier
from librae.core.strategy import PositionSide, PositionState

FUNDING_RATE_FIELD = "funding_rate"
FUNDING_MARK_PRICE_FIELD = "funding_mark_price"
BORROW_RATE_FIELD = "borrow_rate"

FinancingKind = Literal["funding", "borrow"]


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
