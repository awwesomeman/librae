"""Shared adapter metadata and validation helpers.

Concrete adapters (CryptoAdapter, ShioajiAdapter, etc.) are sync and
duck-typed — their capabilities are matched by shape rather than an ABC.
This module only holds metadata and the small validation or rounding helpers
that are identical across adapters; credential loading lives in
librae.config.env so notification adapters can share it without depending
on brokers.

Adapters are plain duck-typed classes with no shared async ABC layer. No
adapter would implement one, and its OrderAdapter would sit next to the
incompatible real one in librae/live/executor.py.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import isfinite
from typing import Any

import numpy as np
import pandas as pd

from librae.core.utils import floor_to_step as floor_to_step

# ---------------------------------------------------------------------------
# get_position() shared shape
# ---------------------------------------------------------------------------


def find_position(
    positions: Iterable[Any],
    symbol: str,
    *,
    matches: Callable[[Any], bool],
    size: Callable[[Any], float],
    avg_price: Callable[[Any], float],
    pnl: Callable[[Any], float] = lambda pos: 0.0,
) -> dict:
    """Resolve exactly one native position into the shared adapter shape.

    Multiple matches are ambiguous because the engine tracks one net position
    per configured instrument. Fail closed instead of silently selecting an
    arbitrary account, contract expiry, or position condition.

    Return the shape every adapter's
    get_position() must return: ``{symbol, size, avg_price, unrealized_pnl}``
    (zeroed if not found).
    """
    matched = [position for position in positions if matches(position)]
    if not matched:
        return {"symbol": symbol, "size": 0, "avg_price": 0, "unrealized_pnl": 0}
    if len(matched) > 1:
        raise ValueError(f"ambiguous broker positions for {symbol}: {len(matched)} matches")
    position = matched[0]
    raw_size = size(position)
    try:
        resolved_size = float(raw_size)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"broker position for {symbol} contains non-numeric size") from exc
    if not isfinite(resolved_size):
        raise ValueError(f"broker position for {symbol} contains non-finite size")
    if resolved_size == 0:
        return {"symbol": symbol, "size": 0.0, "avg_price": 0.0, "unrealized_pnl": 0.0}
    raw_avg_price = avg_price(position)
    raw_pnl = pnl(position)
    try:
        resolved_avg_price = float(raw_avg_price)
        resolved_pnl = float(raw_pnl)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"broker position for {symbol} contains non-numeric facts") from exc
    if not isfinite(resolved_avg_price) or not isfinite(resolved_pnl):
        raise ValueError(f"broker position for {symbol} contains non-finite facts")
    if resolved_avg_price <= 0:
        raise ValueError(f"open broker position for {symbol} requires a positive average price")
    return {
        "symbol": symbol,
        "size": resolved_size,
        "avg_price": resolved_avg_price,
        "unrealized_pnl": resolved_pnl,
    }


# How long past its close a bar that ends its trading segment is held. No
# later bar can show the source finished it before the next segment opens,
# and nothing trades until then, so the hold costs no fill; it only has to
# outlast the source's lag.
SEGMENT_END_SETTLE = pd.Timedelta(minutes=1)


def _segment_end_settle(start: pd.Timestamp, canonical: str, calendar_id: str) -> pd.Timedelta:
    """``SEGMENT_END_SETTLE`` for a bar that ends its trading segment, else zero."""
    from librae.core.readiness import next_expected_close
    from librae.core.trading_calendar import period_close, period_start

    close_at = period_close(start, canonical, calendar_id)
    following_close = pd.Timestamp(
        next_expected_close(start.to_pydatetime(), timeframe=canonical, calendar_id=calendar_id)
    )
    following_start = period_start(
        following_close - pd.Timedelta(1, unit="ns"), canonical, calendar_id
    )
    return SEGMENT_END_SETTLE if following_start > close_at else pd.Timedelta(0)


def settled_available_at(ts: object, timeframe: str, calendar_id: str) -> pd.Timestamp:
    """When the bar at ``ts`` is safe to observe under ``drop_incomplete_ohlcv``'s rule.

    Its close, or ``SEGMENT_END_SETTLE`` past it when it ends its trading
    segment: until then no later bar can show the source finished it. This is
    the bar's ``available_at``, so every mode treats the settle alike.
    """
    from librae.core.trading_calendar import period_close
    from librae.core.utils import to_canonical

    start = pd.Timestamp(ts)
    canonical = to_canonical(timeframe)
    return period_close(start, canonical, calendar_id) + _segment_end_settle(
        start, canonical, calendar_id
    )


def with_settled_available_at(df: pd.DataFrame, timeframe: str, calendar_id: str) -> pd.DataFrame:
    """``df`` with each bar's ``available_at`` from ``settled_available_at``.

    Intraday bars are stamped from their segment closes at once, since a long
    window holds tens of thousands of them and only a segment's last bar can
    settle; an intraday bar outside every segment keeps its nominal close.
    """
    from librae.core.trading_calendar import segment_closes
    from librae.core.utils import interval_to_timedelta, to_canonical

    stamped = df.copy()
    if stamped.empty:
        stamped["available_at"] = pd.Series(dtype="datetime64[ns, UTC]")
        return stamped
    canonical = to_canonical(timeframe)
    starts = pd.DatetimeIndex(pd.to_datetime(stamped["ts"], utc=True))
    if not canonical.startswith(("M", "H")) or canonical.startswith("MN"):
        available = [settled_available_at(start, canonical, calendar_id) for start in starts]
        stamped["available_at"] = pd.DatetimeIndex(available, tz="UTC")
        return stamped
    nominal = starts + interval_to_timedelta(canonical)
    segment_ends = segment_closes(starts, canonical, calendar_id)
    closes = nominal.where(segment_ends.isna() | (nominal <= segment_ends), segment_ends)
    available = closes.to_series(index=range(len(closes)))
    for position in np.flatnonzero(closes == segment_ends):
        available.iloc[position] += _segment_end_settle(starts[position], canonical, calendar_id)
    stamped["available_at"] = available.to_numpy()
    return stamped


def drop_incomplete_ohlcv(
    df: pd.DataFrame,
    timeframe: str,
    *,
    calendar_id: str | None = None,
) -> pd.DataFrame:
    """Drop the newest bar unless the source has shown it closed.

    A source can still be filling a bar after its close has passed on this
    host, behind clock offset or trade aggregation, and a decision taken on
    a short bar cannot be redone. ccxt documents the current candle as one
    that "may be incomplete until the candle is closed (until the next
    candle starts)"; Shioaji minutes do the same (on 2026-11-05 TXFR1's
    11:33 Taipei minute read 81 lots half a second after its close and 91
    once the next minute appeared).

    A later bar in the response shows that the one before it closed.
    Without one, the newest is held until the bar after it would have closed
    too, so a market that has not traded since still gets it; a bar that
    ends its trading segment, which no later bar can follow until the next
    segment opens, only for ``SEGMENT_END_SETTLE``. Without ``calendar_id``
    every period follows the last.
    """
    if df.empty:
        return df
    from librae.core.utils import interval_to_timedelta, to_canonical

    newest = pd.Timestamp(df["ts"].iloc[-1])
    canonical = to_canonical(timeframe)
    if calendar_id is not None:
        from librae.core.readiness import next_expected_close

        if _segment_end_settle(newest, canonical, calendar_id):
            release_at = settled_available_at(newest, canonical, calendar_id)
        else:
            release_at = pd.Timestamp(
                next_expected_close(
                    newest.to_pydatetime(), timeframe=canonical, calendar_id=calendar_id
                )
            )
    else:
        period = (
            pd.offsets.MonthBegin(int(canonical[2:]))
            if canonical.startswith("MN")
            else interval_to_timedelta(canonical)
        )
        release_at = newest + period + period
    if release_at > datetime.now(UTC):
        return df.iloc[:-1]
    return df


def validate_order_signal(signal: Mapping[str, Any]) -> None:
    """Reject ambiguous canonical order fields before venue conversion."""
    symbol = signal.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("symbol must be a non-empty string")
    side = signal.get("side")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    order_type = signal.get("order_type")
    if order_type not in ("market", "limit"):
        raise ValueError("order_type must be 'market' or 'limit'")
    if signal.get("time_in_force") not in ("day", "gtc", "ioc", "fok"):
        raise ValueError("time_in_force must be 'day', 'gtc', 'ioc', or 'fok'")
    # Adapters derive exit safety from it (e.g. reduce-only), so a missing or
    # misspelled value must not degrade silently into an unguarded order.
    if signal.get("position_effect") not in ("open", "add", "reduce", "close"):
        raise ValueError("position_effect must be 'open', 'add', 'reduce', or 'close'")
    try:
        quantity = float(signal["quantity"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("quantity must be positive and finite") from exc
    if not isfinite(quantity) or quantity <= 0:
        raise ValueError("quantity must be positive and finite")
    if order_type == "limit":
        try:
            price = float(signal["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("limit price must be positive and finite") from exc
        if not isfinite(price) or price <= 0:
            raise ValueError("limit price must be positive and finite")


def validate_time_in_force(
    broker: str,
    supported: Mapping[str, frozenset[str]],
    order_type: str,
    time_in_force: str,
) -> None:
    """Reject a lifetime the venue cannot express for this order type.

    Takes the table rather than looking one up, so the adapter enforces the
    same constant it publishes and the two cannot drift. The lookup by broker
    name lives in librae.brokers.capabilities.
    """
    accepted = supported.get(order_type, frozenset())
    if time_in_force not in accepted:
        raise ValueError(
            f"{broker} does not support time_in_force={time_in_force!r} on a "
            f"{order_type} order; supported: {sorted(accepted)}"
        )


def passive_price(price: float, tick_size: float, side: str) -> float:
    """Round a limit price without making it more aggressive."""
    if not isfinite(price) or price <= 0:
        raise ValueError("price must be positive and finite")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    rounding = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    units = (Decimal(str(price)) / Decimal(str(tick_size))).to_integral_value(rounding=rounding)
    return float(units * Decimal(str(tick_size)))


# ---------------------------------------------------------------------------
# Canonical data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterInfo:
    """Static metadata about an adapter instance."""

    adapter_id: str
    venue: str
    market_type: str
    schema_version: str = "1.0.0"
