"""Shared adapter metadata and credential loading.

Concrete adapters (CryptoAdapter, ShioajiAdapter, etc.) are sync and
duck-typed — their capabilities are matched by shape rather than an ABC.
This module only holds metadata, credential loading, and the small validation
or rounding helpers that are identical across adapters.

(An async ABC layer — MarketDataAdapter/OrderAdapter/AccountAdapter plus
canonical L1Quote/TradeTick/Bar/Order/Fill/Position types — used to live
here for a future real-time/multi-venue design, but no adapter ever
implemented it; removed to avoid a second, incompatible "OrderAdapter"
next to the real one in librae/live/executor.py.)
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import isfinite
from typing import Any, Self

import pandas as pd

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
    """Scan *positions* for *symbol* and return the shape every adapter's
    get_position() must return: ``{symbol, size, avg_price, unrealized_pnl}``
    (zeroed if not found). Shared by CryptoAdapter/ShioajiAdapter/IBKRAdapter,
    which differ only in how to pull these fields off their own native
    position object.
    """
    for pos in positions:
        if matches(pos):
            return {
                "symbol": symbol,
                "size": size(pos),
                "avg_price": avg_price(pos),
                "unrealized_pnl": pnl(pos),
            }
    return {"symbol": symbol, "size": 0, "avg_price": 0, "unrealized_pnl": 0}


def drop_incomplete_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Drop a final candle whose interval has not closed yet."""
    if df.empty:
        return df
    from librae.core.utils import interval_to_timedelta

    last_ts = pd.Timestamp(df["ts"].iloc[-1]).to_pydatetime()
    if last_ts > datetime.now(UTC) - interval_to_timedelta(timeframe):
        return df.iloc[:-1]
    return df


def floor_to_step(value: float, step: float) -> float:
    """Round a positive quantity down to an exchange-supported step."""
    if not isfinite(value) or value <= 0 or not isfinite(step) or step <= 0:
        raise ValueError("value and step must be positive and finite")
    units = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * Decimal(str(step)))


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


# ---------------------------------------------------------------------------
# Credential config
# ---------------------------------------------------------------------------


@dataclass
class CredentialConfig:
    """Base credential config with env-var loading.

    Subclass and add fields for each venue.  Call ``from_env()`` to
    populate fields from environment variables automatically.

    Env-var mapping convention: ``{PREFIX}_{FIELD_UPPER}``.
    E.g. ``BINANCE_API_KEY`` for field ``api_key`` with prefix ``BINANCE``.
    """

    @classmethod
    def from_env(cls, prefix: str, **overrides: str) -> Self:
        """Build credentials from environment variables.

        For each dataclass field, looks up ``{prefix}_{FIELD_UPPER}``
        in ``os.environ``.  Explicit *overrides* take precedence.
        """
        _sentinel = object()
        kwargs: dict = {}
        for f in dataclasses.fields(cls):
            override = overrides.get(f.name, _sentinel)
            if override is not _sentinel:
                kwargs[f.name] = override
            else:
                env_val = os.environ.get(f"{prefix}_{f.name.upper()}")
                if env_val is not None:
                    kwargs[f.name] = env_val
        return cls(**kwargs)
