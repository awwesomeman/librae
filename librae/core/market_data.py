"""Shared market-data identity and bar validation contracts."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from librae.core.run_config import MarketDataSessionMode
from librae.core.trading_calendar import ALWAYS_OPEN_CALENDAR, period_close, period_start
from librae.core.utils import interval_to_timedelta, to_canonical

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
CAN_BUY_COLUMN = "can_buy"
CAN_SELL_COLUMN = "can_sell"
SIDE_TRADABILITY_COLUMNS = (CAN_BUY_COLUMN, CAN_SELL_COLUMN)
AVAILABLE_AT_COLUMN = "available_at"


@dataclass(frozen=True, slots=True, order=True)
class MarketDataSubscription:
    """Immutable identity of one homogeneous OHLCV subscription."""

    symbol: str
    timeframe: str
    calendar_id: str
    session_mode: MarketDataSessionMode
    data_source: str
    instrument_type: str

    def __post_init__(self) -> None:
        for field_name in ("symbol", "calendar_id", "data_source", "instrument_type"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        object.__setattr__(self, "timeframe", to_canonical(self.timeframe))
        if self.session_mode not in ("regular", "extended"):
            raise ValueError(
                f"session_mode must be 'regular' or 'extended', got {self.session_mode!r}"
            )
        from librae.config.symbols import validate_instrument_type

        validate_instrument_type(self.instrument_type)
        from librae.core.trading_calendar import validate_calendar_id

        validate_calendar_id(self.calendar_id)

    def to_dict(self) -> dict[str, str]:
        """Return the stable JSON/manifest representation of this identity."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "calendar_id": self.calendar_id,
            "session_mode": self.session_mode,
            "data_source": self.data_source,
            "instrument_type": self.instrument_type,
        }

    @classmethod
    def from_dict(cls, value: object) -> MarketDataSubscription:
        """Parse one persisted identity without filling missing legacy fields."""
        if not isinstance(value, dict):
            raise ValueError("market-data subscription must be an object")
        required = {
            "symbol",
            "timeframe",
            "calendar_id",
            "session_mode",
            "data_source",
            "instrument_type",
        }
        if set(value) != required:
            raise ValueError(
                "market-data subscription fields must be exactly "
                f"{sorted(required)}; got {sorted(value)}"
            )
        return cls(**value)


def subscription_from_instrument(
    instrument: object,
    *,
    timeframe: str,
    session_mode: MarketDataSessionMode,
) -> MarketDataSubscription:
    """Build a bar identity from one already-resolved ``SymbolInfo``-like value."""
    calendar_id = getattr(instrument, "calendar_id", None)
    symbol = getattr(instrument, "symbol", None)
    if calendar_id is None:
        raise ValueError(f"market-data subscription for {symbol!r} requires a resolved calendar_id")
    return MarketDataSubscription(
        symbol=symbol,
        timeframe=timeframe,
        calendar_id=calendar_id,
        session_mode=session_mode,
        data_source=getattr(instrument, "data_source", None),
        instrument_type=getattr(instrument, "instrument_type", None),
    )


def _utc_index(values: object, *, field_name: str) -> pd.DatetimeIndex:
    try:
        timestamps = pd.DatetimeIndex(pd.to_datetime(values, errors="raise"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} values must be valid timestamps") from exc
    if timestamps.tz is None:
        raise ValueError(f"{field_name} values must be timezone-aware")
    if timestamps.hasnans:
        raise ValueError(f"{field_name} values must not contain NaT")
    return timestamps.tz_convert("UTC")


def _completion_floor(
    timestamp: pd.Timestamp,
    subscription: MarketDataSubscription,
    *,
    allow_extended_calendar_inference: bool,
) -> pd.Timestamp:
    timeframe = subscription.timeframe
    calendar_sized = timeframe.startswith(("D", "W", "MN"))
    extended_calendar = (
        subscription.session_mode == "extended" and subscription.calendar_id != ALWAYS_OPEN_CALENDAR
    )
    if calendar_sized and extended_calendar and not allow_extended_calendar_inference:
        raise ValueError(
            "available_at is required for extended-session calendar bars because "
            f"calendar_id={subscription.calendar_id!r} does not define their close"
        )
    if not calendar_sized and extended_calendar:
        # A regular-session calendar cannot validate after-hours bucket geometry.
        # The nominal fixed duration is conservative for a shortened final bucket.
        return timestamp + interval_to_timedelta(timeframe)
    try:
        expected_start = period_start(timestamp, timeframe, subscription.calendar_id)
        floor = period_close(timestamp, timeframe, subscription.calendar_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"cannot establish completion for {subscription.symbol!r} "
            f"timeframe={timeframe} at {timestamp.isoformat()}"
        ) from exc
    if timestamp != expected_start:
        raise ValueError(
            f"bar timestamp {timestamp.isoformat()} is not the canonical "
            f"timeframe={timeframe} start for calendar_id={subscription.calendar_id!r}"
        )
    return floor


def normalize_bar_times(
    timestamps: object,
    available_at: object | None,
    subscription: MarketDataSubscription,
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """Normalize one subscription's row clocks and enforce causal availability."""
    normalized_ts = _utc_index(timestamps, field_name="bar timestamps")
    if not normalized_ts.is_monotonic_increasing:
        raise ValueError("bar timestamps must be increasing within a subscription")
    provided = available_at is not None
    normalized_available = (
        _utc_index(available_at, field_name=AVAILABLE_AT_COLUMN) if provided else None
    )
    if normalized_available is not None and len(normalized_available) != len(normalized_ts):
        raise ValueError("available_at must have one value per bar timestamp")

    floors = pd.DatetimeIndex(
        [
            _completion_floor(
                timestamp,
                subscription,
                allow_extended_calendar_inference=provided,
            )
            for timestamp in normalized_ts
        ]
    )
    if normalized_available is None:
        normalized_available = floors
    elif np.any(normalized_available < floors):
        first = int(np.flatnonzero(normalized_available < floors)[0])
        raise ValueError(
            f"available_at {normalized_available[first].isoformat()} is earlier than "
            f"bar completion {floors[first].isoformat()} for {subscription.symbol!r}"
        )
    if not normalized_available.is_monotonic_increasing:
        raise ValueError("available_at must not move backward within a subscription")
    return normalized_ts, normalized_available


def validate_ohlcv_values(data: pd.DataFrame, *, context: str = "data") -> None:
    """Fail fast on OHLCV values that cannot safely drive fills or accounting."""
    missing = sorted(set(OHLCV_COLUMNS) - set(data.columns))
    if missing:
        raise ValueError(f"{context} missing required OHLCV columns: {', '.join(missing)}")
    if data.empty:
        raise ValueError(f"{context} must contain at least one OHLCV bar")

    non_numeric = [
        column for column in OHLCV_COLUMNS if not pd.api.types.is_numeric_dtype(data[column])
    ]
    if non_numeric:
        raise ValueError(f"{context} OHLCV columns must be numeric: {', '.join(non_numeric)}")

    values = data.loc[:, list(OHLCV_COLUMNS)].to_numpy(dtype=np.float64, na_value=np.nan)
    if not np.isfinite(values).all():
        raise ValueError(f"{context} OHLCV values must be finite")

    open_price, high, low, close, volume = values.T
    if np.any(open_price <= 0) or np.any(high <= 0) or np.any(low <= 0) or np.any(close <= 0):
        raise ValueError(f"{context} OHLC prices must be positive")
    if np.any(high < np.maximum.reduce([open_price, low, close])) or np.any(
        low > np.minimum.reduce([open_price, high, close])
    ):
        raise ValueError(
            f"{context} OHLC values are inconsistent: low <= open/close <= high is required"
        )
    if np.any(volume < 0):
        raise ValueError(f"{context} volume must be non-negative")

    present_tradability_columns = [
        column for column in SIDE_TRADABILITY_COLUMNS if column in data.columns
    ]
    if present_tradability_columns and len(present_tradability_columns) != len(
        SIDE_TRADABILITY_COLUMNS
    ):
        raise ValueError(f"{context} must provide can_buy and can_sell together")
    for column in present_tradability_columns:
        if not pd.api.types.is_bool_dtype(data[column]) or data[column].isna().any():
            raise ValueError(f"{context} {column} must contain non-null booleans")
