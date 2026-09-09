"""Shared adapter metadata and validation helpers.

Concrete adapters (CryptoAdapter, ShioajiAdapter, etc.) are sync and
duck-typed — their capabilities are matched by shape rather than an ABC.
This module only holds metadata and the small validation or rounding helpers
that are identical across adapters; credential loading lives in
librae.config.env so notification adapters can share it without depending
on brokers.

(An async ABC layer — MarketDataAdapter/OrderAdapter/AccountAdapter plus
canonical L1Quote/TradeTick/Bar/Order/Fill/Position types — used to live
here for a future real-time/multi-venue design, but no adapter ever
implemented it; removed to avoid a second, incompatible "OrderAdapter"
next to the real one in librae/live/executor.py.)
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import isfinite
from typing import Any

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


def drop_incomplete_ohlcv(
    df: pd.DataFrame,
    timeframe: str,
    *,
    calendar_id: str | None = None,
) -> pd.DataFrame:
    """Drop a final bar-start candle whose interval has not closed yet."""
    if df.empty:
        return df
    from librae.core.utils import interval_to_timedelta, to_canonical

    last_ts = pd.Timestamp(df["ts"].iloc[-1])
    canonical = to_canonical(timeframe)
    if calendar_id is not None:
        from librae.core.trading_calendar import period_close

        close_at = period_close(last_ts, canonical, calendar_id)
    elif canonical.startswith("MN"):
        month_count = int(canonical[2:])
        close_at = last_ts + pd.offsets.MonthBegin(month_count)
    else:
        close_at = last_ts + interval_to_timedelta(canonical)
    if close_at > datetime.now(UTC):
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
