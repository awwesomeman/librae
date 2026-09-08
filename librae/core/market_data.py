"""Shared market-data identity and bar validation contracts."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from librae.core.run_config import MarketDataSessionMode
from librae.core.trading_calendar import (
    ALWAYS_OPEN_CALENDAR,
    period_close,
    period_start,
    session_labels,
    session_ordinals,
)
from librae.core.utils import interval_to_timedelta, to_canonical

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
CAN_BUY_COLUMN = "can_buy"
CAN_SELL_COLUMN = "can_sell"
SIDE_TRADABILITY_COLUMNS = (CAN_BUY_COLUMN, CAN_SELL_COLUMN)
AVAILABLE_AT_COLUMN = "available_at"


def _detach_object_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy a frame and recursively detach mutable object-dtype feature cells."""
    detached = frame.copy(deep=True)
    for position, dtype in enumerate(detached.dtypes):
        if pd.api.types.is_object_dtype(dtype):
            detached.iloc[:, position] = detached.iloc[:, position].map(deepcopy)
    return detached


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
        for field_name in (
            "symbol",
            "timeframe",
            "calendar_id",
            "session_mode",
            "data_source",
            "instrument_type",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
            if value != value.strip():
                raise ValueError(f"{field_name} must not contain leading or trailing whitespace")
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


class _MarketDataSource:
    """Run-owned immutable source used by lazy point-in-time views."""

    __slots__ = ("_frames", "_index", "_monotonic", "subscriptions")

    def __init__(self, frames: Mapping[MarketDataSubscription, pd.DataFrame]) -> None:
        subscriptions = tuple(sorted(frames))
        self.subscriptions = subscriptions
        self._frames = tuple(_detach_object_features(frames[item]) for item in subscriptions)
        self._monotonic = tuple(frame.index.is_monotonic_increasing for frame in self._frames)
        self._index = {subscription: index for index, subscription in enumerate(subscriptions)}

    def __deepcopy__(self, memo: dict[int, object]) -> _MarketDataSource:
        # The source is engine-owned and has no mutating API. Views carry only
        # immutable prefix counts, so sharing it cannot advance an old view.
        return self

    def index_of(self, subscription: MarketDataSubscription) -> int:
        try:
            return self._index[subscription]
        except KeyError as exc:
            raise KeyError(f"unknown market-data subscription: {subscription!r}") from exc

    def row_count(self, index: int) -> int:
        return len(self._frames[index])

    def history(
        self,
        subscription: MarketDataSubscription,
        visible_count: int,
        limit: int | None,
    ) -> pd.DataFrame:
        index = self.index_of(subscription)
        frame = self._frames[index]
        visible = frame.iloc[:visible_count]
        if self._monotonic[index]:
            if limit is not None:
                visible = visible.tail(limit)
        else:
            visible = visible.sort_index(kind="stable")
            if limit is not None:
                visible = visible.tail(limit)
        return _detach_object_features(visible)


@dataclass(frozen=True, slots=True)
class MarketDataView:
    """Point-in-time, identity-keyed market history exposed to a strategy.

    Views are lazy snapshots: immutable visibility counts freeze the as-of
    frontier, while ``history`` materializes only selected rows and returns a
    deep copy. Later engine progress and caller mutation therefore cannot
    change an already emitted ``Context``.
    """

    as_of: datetime
    _source: _MarketDataSource = field(repr=False)
    _visible_counts: tuple[int, ...] = field(repr=False)
    _history_limit: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        timestamp = pd.Timestamp(self.as_of)
        if timestamp.tz is None:
            raise ValueError("MarketDataView.as_of must be timezone-aware")
        if not isinstance(self._source, _MarketDataSource):
            raise TypeError("MarketDataView source must be an engine market-data source")
        visible_counts = tuple(self._visible_counts)
        if len(visible_counts) != len(self._source.subscriptions):
            raise ValueError("MarketDataView visibility must cover every subscription")
        for index, count in enumerate(visible_counts):
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
                or count > self._source.row_count(index)
            ):
                raise ValueError("MarketDataView visibility counts are invalid")
        if self._history_limit is not None and (
            isinstance(self._history_limit, bool)
            or not isinstance(self._history_limit, int)
            or self._history_limit <= 0
        ):
            raise ValueError("MarketDataView history limit must be a positive integer or None")
        object.__setattr__(self, "as_of", timestamp.tz_convert("UTC").to_pydatetime())
        object.__setattr__(self, "_visible_counts", visible_counts)

    @property
    def subscriptions(self) -> tuple[MarketDataSubscription, ...]:
        """Return the exact identities available through this view."""
        return self._source.subscriptions

    def history(
        self,
        subscription: MarketDataSubscription,
        *,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Return visible rows for one exact identity in canonical time order."""
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        ):
            raise ValueError("limit must be a positive integer or None")
        index = self._source.index_of(subscription)
        effective_limit = limit
        if self._history_limit is not None:
            effective_limit = (
                self._history_limit
                if effective_limit is None
                else min(effective_limit, self._history_limit)
            )
        return self._source.history(subscription, self._visible_counts[index], effective_limit)

    def _with_history_limit(self, limit: int) -> MarketDataView:
        """Return the same frozen frontier with a bounded history window."""
        return MarketDataView(
            as_of=self.as_of,
            _source=self._source,
            _visible_counts=self._visible_counts,
            _history_limit=limit,
        )

    @classmethod
    def _from_causal_frames(
        cls,
        frames: Mapping[MarketDataSubscription, pd.DataFrame],
        *,
        as_of: datetime,
        history_limit: int | None = None,
    ) -> MarketDataView:
        """Build an engine-owned view from histories already filtered to ``as_of``."""
        source = _MarketDataSource(frames)
        return cls(
            as_of=as_of,
            _source=source,
            _visible_counts=tuple(
                source.row_count(index) for index in range(len(source.subscriptions))
            ),
            _history_limit=history_limit,
        )


@dataclass(frozen=True, slots=True)
class FeatureBatch:
    """Immutable point-in-time input for one cross-asset feature evaluation."""

    event_ts: datetime
    as_of: datetime
    primary_subscriptions: tuple[MarketDataSubscription, ...]
    active_primary_subscriptions: tuple[MarketDataSubscription, ...]
    market_data: MarketDataView

    def __post_init__(self) -> None:
        event_ts = pd.Timestamp(self.event_ts)
        as_of = pd.Timestamp(self.as_of)
        if event_ts.tz is None:
            raise ValueError("FeatureBatch.event_ts must be timezone-aware")
        if as_of.tz is None:
            raise ValueError("FeatureBatch.as_of must be timezone-aware")
        event_ts = event_ts.tz_convert("UTC")
        as_of = as_of.tz_convert("UTC")
        if as_of < event_ts:
            raise ValueError("FeatureBatch.as_of must not be earlier than event_ts")

        try:
            primary = tuple(self.primary_subscriptions)
        except TypeError as exc:
            raise TypeError("FeatureBatch.primary_subscriptions must be a sequence") from exc
        try:
            active = tuple(self.active_primary_subscriptions)
        except TypeError as exc:
            raise TypeError("FeatureBatch.active_primary_subscriptions must be a sequence") from exc
        if any(not isinstance(item, MarketDataSubscription) for item in primary):
            raise TypeError(
                "FeatureBatch.primary_subscriptions must contain MarketDataSubscription values"
            )
        if not primary:
            raise ValueError("FeatureBatch.primary_subscriptions must not be empty")
        if len(set(primary)) != len(primary):
            raise ValueError("FeatureBatch.primary_subscriptions must not contain duplicates")
        if any(not isinstance(item, MarketDataSubscription) for item in active):
            raise TypeError(
                "FeatureBatch.active_primary_subscriptions must contain "
                "MarketDataSubscription values"
            )
        if not active:
            raise ValueError("FeatureBatch.active_primary_subscriptions must not be empty")
        if len(set(active)) != len(active):
            raise ValueError(
                "FeatureBatch.active_primary_subscriptions must not contain duplicates"
            )
        active_set = set(active)
        if not active_set.issubset(primary):
            raise ValueError("FeatureBatch active subscriptions must be primary subscriptions")
        if active != tuple(item for item in primary if item in active_set):
            raise ValueError("FeatureBatch active subscriptions must follow primary order")
        if not isinstance(self.market_data, MarketDataView):
            raise TypeError("FeatureBatch.market_data must be a MarketDataView")
        if pd.Timestamp(self.market_data.as_of) != as_of:
            raise ValueError("FeatureBatch.market_data must use the batch as_of frontier")
        if not set(primary).issubset(self.market_data.subscriptions):
            raise ValueError("FeatureBatch.market_data must contain every primary subscription")

        object.__setattr__(self, "event_ts", event_ts.to_pydatetime())
        object.__setattr__(self, "as_of", as_of.to_pydatetime())
        object.__setattr__(self, "primary_subscriptions", primary)
        object.__setattr__(self, "active_primary_subscriptions", active)


type BatchFeatureOutput = Mapping[MarketDataSubscription, pd.DataFrame]
type BatchFeatureFn = Callable[[FeatureBatch], BatchFeatureOutput]


def validate_feature_frame(
    output: object,
    *,
    label: str,
    event_ts: datetime,
    visible_index: pd.DatetimeIndex | None = None,
) -> pd.DataFrame:
    """Require one causal, current-event feature frame."""
    if not isinstance(output, pd.DataFrame):
        raise TypeError(f"{label} feature callback must return a pandas DataFrame")
    if output.empty:
        raise ValueError(f"{label} feature output must not be empty")
    if not isinstance(output.index, pd.DatetimeIndex):
        raise ValueError(f"{label} feature output must use a DatetimeIndex")

    index = output.index
    if index.tz is None:
        raise ValueError(f"{label} feature output index must be timezone-aware")
    if index.hasnans:
        raise ValueError(f"{label} feature output index must not contain NaT")
    if not index.is_unique:
        raise ValueError(f"{label} feature output timestamps must be unique")
    if not index.is_monotonic_increasing:
        raise ValueError(f"{label} feature output timestamps must be strictly increasing")

    event_timestamp = pd.Timestamp(event_ts)
    if bool((index > event_timestamp).any()):
        raise ValueError(f"{label} feature output contains a timestamp after event {event_ts}")
    if index[-1] != event_timestamp:
        raise ValueError(
            f"{label} feature output final timestamp {index[-1]} does not match event {event_ts}"
        )
    if visible_index is not None and not bool(index.isin(visible_index).all()):
        raise ValueError(f"{label} feature output contains a timestamp outside its causal history")
    return output


def evaluate_batch_features(
    batch_feature_fn: BatchFeatureFn,
    batch: FeatureBatch,
) -> dict[MarketDataSubscription, pd.DataFrame]:
    """Evaluate and atomically validate one subscription-keyed feature batch."""
    output = batch_feature_fn(batch)
    if not isinstance(output, Mapping):
        raise TypeError("batch_feature_fn must return a mapping")
    if any(not isinstance(item, MarketDataSubscription) for item in output):
        raise TypeError("batch_feature_fn output keys must be MarketDataSubscription values")
    expected = set(batch.active_primary_subscriptions)
    actual = set(output)
    if actual != expected or len(output) != len(expected):
        missing = tuple(item for item in batch.active_primary_subscriptions if item not in actual)
        extra = tuple(item for item in output if item not in expected)
        raise ValueError(
            "batch_feature_fn output must exactly cover active primary subscriptions; "
            f"missing={missing!r}, extra={extra!r}"
        )

    validated: dict[MarketDataSubscription, pd.DataFrame] = {}
    for subscription in batch.active_primary_subscriptions:
        visible_index = batch.market_data.history(subscription).index
        validated[subscription] = _detach_object_features(
            validate_feature_frame(
                output[subscription],
                label=repr(subscription),
                event_ts=batch.event_ts,
                visible_index=visible_index,
            )
        )
    return validated


def subscription_from_instrument(
    instrument: object,
    *,
    timeframe: str,
    session_mode: MarketDataSessionMode,
    calendar_id: str | None = None,
) -> MarketDataSubscription:
    """Build a bar identity from one already-resolved ``SymbolInfo``-like value."""
    if calendar_id is None:
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
        raw_values = list(values)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} values must be valid timestamps") from exc

    normalized: list[pd.Timestamp] = []
    for value in raw_values:
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} values must be valid timestamps") from exc
        if pd.isna(timestamp):
            raise ValueError(f"{field_name} values must not contain NaT")
        if timestamp.tz is None:
            raise ValueError(f"{field_name} values must be timezone-aware")
        normalized.append(timestamp.tz_convert("UTC"))
    if not normalized:
        return pd.DatetimeIndex([], tz="UTC")
    return pd.DatetimeIndex(normalized)


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


def validate_bar_cadence(
    timestamps: pd.DatetimeIndex,
    timeframe: str,
    calendar_id: str,
    *,
    context: str = "bar data",
) -> None:
    """Validate non-overlap plus calendar-sized per-series cadence.

    Fixed intraday bars need not share a provider-independent global phase,
    but two observations of one subscription cannot overlap. Calendar-sized
    bars additionally use the first observation as that series' phase. Missing
    whole bars are allowed; different symbols remain free to use other phases.
    """
    canonical = to_canonical(timeframe)
    if canonical.startswith(("M", "H")) and not canonical.startswith("MN"):
        interval = interval_to_timedelta(canonical)
        diffs = pd.Series(timestamps).diff().dropna()
        if bool((diffs < interval).any()):
            raise ValueError(f"{context} timestamps overlap timeframe={canonical}")
        return
    if not canonical.startswith(("D", "W", "MN")):
        return

    expected_starts = pd.DatetimeIndex(
        [period_start(timestamp, canonical, calendar_id) for timestamp in timestamps]
    )
    if np.any(timestamps != expected_starts):
        raise ValueError(
            f"{context} timestamps are not the canonical timeframe={canonical} period starts"
        )

    if canonical.startswith("D"):
        interval = int(canonical[1:])
        ordinals = np.asarray(session_ordinals(timestamps, calendar_id), dtype=np.int64)
    else:
        labels = session_labels(timestamps, calendar_id)
        if canonical.startswith("W"):
            interval = int(canonical[1:])
            ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="W-SUN").asi8
        else:
            interval = int(canonical[2:])
            ordinals = pd.PeriodIndex(pd.to_datetime(labels), freq="M").asi8
    diffs = np.diff(ordinals)
    if np.any(diffs < interval) or np.any(diffs % interval != 0):
        raise ValueError(f"{context} timestamps are not aligned to timeframe={canonical}")


def normalize_bar_times(
    timestamps: object,
    available_at: object | None,
    subscription: MarketDataSubscription,
) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """Normalize row clocks and validate the currently supplied row versions.

    ``available_at`` is version-local: a later correction to an older bar may
    legitimately have a later availability than a newer bar.  It therefore
    has no cross-row monotonicity requirement.
    """
    normalized_ts = _utc_index(timestamps, field_name="bar timestamps")
    if not normalized_ts.is_monotonic_increasing:
        raise ValueError("bar timestamps must be increasing within a subscription")
    validate_bar_cadence(
        normalized_ts,
        subscription.timeframe,
        subscription.calendar_id,
        context=f"{subscription.symbol!r} subscription",
    )
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
