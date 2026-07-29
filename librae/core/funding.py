"""Deterministic perpetual-funding cash-flow accounting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from math import isfinite, isnan

from librae.core.cost_model import CostModel
from librae.core.executor import side_multiplier
from librae.core.strategy import PositionSide, PositionState

FUNDING_RATE_FIELD = "funding_rate"
FUNDING_MARK_PRICE_FIELD = "funding_mark_price"


@dataclass(frozen=True, slots=True)
class FundingCashFlow:
    """One funding payment calculated from a confirmed open position."""

    ts: datetime
    symbol: str
    side: PositionSide
    quantity: float
    mark_price: float
    multiplier: float
    rate: float
    cash_flow: float


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


def calculate_funding_cash_flows(
    ts: datetime,
    bars: Mapping[str, Mapping[str, object]],
    positions: Mapping[str, PositionState],
    *,
    get_cost_model: Callable[[str], CostModel],
) -> tuple[tuple[str, ...], list[FundingCashFlow]]:
    """Calculate funding payments present on one market-data event.

    ``funding_rate`` is a decimal rate per payment. A positive rate means
    longs pay shorts. ``funding_mark_price`` is optional and otherwise falls
    back to the same event's raw close. Missing rates are not forward-filled.

    Returns the symbols whose observations were consumed and the payments for
    symbols with confirmed open positions.
    """
    observed_symbols: list[str] = []
    cash_flows: list[FundingCashFlow] = []
    for symbol in sorted(bars):
        bar = bars[symbol]
        if FUNDING_RATE_FIELD not in bar:
            continue
        rate = _optional_finite_float(
            bar[FUNDING_RATE_FIELD],
            field=FUNDING_RATE_FIELD,
            symbol=symbol,
        )
        if rate is None:
            continue
        observed_symbols.append(symbol)

        mark_price = _optional_finite_float(
            bar.get(FUNDING_MARK_PRICE_FIELD),
            field=FUNDING_MARK_PRICE_FIELD,
            symbol=symbol,
        )
        if mark_price is None:
            mark_price = _optional_finite_float(
                bar.get("close"),
                field="close",
                symbol=symbol,
            )
        if mark_price is None or mark_price <= 0:
            raise ValueError(f"{symbol} funding mark price must be finite and positive")

        position = positions.get(symbol)
        if position is None:
            continue
        cost_model = get_cost_model(symbol)
        notional = position.quantity * mark_price * cost_model.multiplier
        cash_flow = -side_multiplier(position.side) * notional * rate
        if not isfinite(notional) or not isfinite(cash_flow):
            raise ValueError(f"{symbol} funding cash flow must be finite")
        cash_flows.append(
            FundingCashFlow(
                ts=ts,
                symbol=symbol,
                side=position.side,
                quantity=float(position.quantity),
                mark_price=mark_price,
                multiplier=float(cost_model.multiplier),
                rate=rate,
                cash_flow=float(cash_flow),
            )
        )
    return tuple(observed_symbols), cash_flows
