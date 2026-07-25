"""Core utility functions shared by backtest and live runtimes.

Provides:
- generate_run_id(): Deterministic-prefix run ID generation
- infer_timeframe(): Infer canonical timeframe label from data index
- to_ccxt() / to_canonical(): Timeframe format conversion
- interval_to_timedelta(): Timeframe string → pd.Timedelta
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import pandas as pd

# ---------------------------------------------------------------------------
# Run / Trade ID
# ---------------------------------------------------------------------------


def _sanitize_slug(name: str) -> str:
    """Lowercase and replace spaces with underscores for use in IDs."""
    return name.lower().replace(" ", "_")


def generate_run_id(strategy: str, symbol: str, timeframe: str | None = None) -> str:
    """Deterministic-prefix run_id: <strategy>-<symbol>[-<timeframe>]-<ts>-<short_uuid>."""
    ts = datetime.now(tz=UTC).strftime("%Y%m%dt%H%M")
    short = uuid.uuid4().hex[:6]
    tf = f"-{_sanitize_slug(timeframe)}" if timeframe is not None else ""
    return f"{_sanitize_slug(strategy)}-{_sanitize_slug(symbol)}{tf}-{ts}-{short}"


def make_event_id(run_id: str, index: int) -> str:
    """Canonical event ID: {run_id}-e{index:04d}."""
    return f"{run_id}-e{index:04d}"


# ---------------------------------------------------------------------------
# Timeframe utilities
# ---------------------------------------------------------------------------
#
# Two formats are supported throughout the codebase:
#
#   Canonical label  │  Examples          │  Used in: DB, config, internal APIs
#   ─────────────────┼────────────────────┼──────────────────────────────────
#   {PREFIX}{N}      │  M1 M5 M30 H1 H4   │  prefix letters = unit, N = count
#                    │  D1 W1 MN1         │
#
#   ccxt / exchange  │  Examples          │  Used in: exchange APIs (Binance etc.)
#   ─────────────────┼────────────────────┼──────────────────────────────────
#   {N}{unit}        │  1m 5m 30m 1h 4h   │  unit letter = m/h/d/w/M
#                    │  1d 1w 1M          │
#
# Mapping between the two is done via _parse_timeframe() which uses regex,
# so any valid combination (M30, H6, H12, D3, W2, MN3 …) works without
# requiring an explicit entry in a lookup table.

_CCXT_RE = re.compile(r"^(\d+)([mhdwM])$")
_CANONICAL_RE = re.compile(r"^([A-Z]+)(\d+)$")

# Canonical prefix → ccxt unit character
_PREFIX_TO_CCXT_UNIT: dict[str, str] = {
    "M": "m",  # minutes
    "H": "h",  # hours
    "D": "d",  # days
    "W": "w",  # weeks
    "MN": "M",  # months  (ccxt uses uppercase M)
}
_CCXT_UNIT_TO_PREFIX: dict[str, str] = {v: k for k, v in _PREFIX_TO_CCXT_UNIT.items()}

# Canonical prefix → lambda(n) → pd.Timedelta
_PREFIX_TO_TIMEDELTA = {
    "M": lambda n: pd.Timedelta(minutes=n),
    "H": lambda n: pd.Timedelta(hours=n),
    "D": lambda n: pd.Timedelta(days=n),
    "W": lambda n: pd.Timedelta(weeks=n),
    # Monthly is approximate (avg 30.44 days); use with care for gap detection
    "MN": lambda n: pd.Timedelta(days=round(n * 30.44)),
}


def _parse_timeframe(interval: str) -> tuple[int, str]:
    """Parse any timeframe string to ``(n, canonical_prefix)``.

    Accepts ccxt format (``'30m'``, ``'4h'``, ``'1M'``) and canonical labels
    (``'M30'``, ``'H4'``, ``'MN1'``).  Any positive integer N is valid, so
    non-standard intervals like H6, H12, M45 are handled without code changes.

    Returns
    -------
    n : int
        The multiplier (e.g. 30 for M30 / 30m).
    canonical_prefix : str
        The canonical unit prefix (e.g. ``'M'`` for minutes, ``'H'`` for hours).

    Raises
    ------
    ValueError
        If the string does not match either format or the unit is unrecognized.
    """
    # ccxt format: digits + single unit char (e.g. "30m", "1h", "1M")
    m = _CCXT_RE.match(interval)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        prefix = _CCXT_UNIT_TO_PREFIX.get(unit)
        if prefix is None:
            raise ValueError(
                f"Unrecognized ccxt unit '{unit}' in '{interval}'. "
                f"Supported: {list(_CCXT_UNIT_TO_PREFIX)}"
            )
        return n, prefix

    # Canonical format: letter prefix + digits (e.g. "M30", "H4", "MN1")
    m = _CANONICAL_RE.match(interval.upper())
    if m:
        prefix = m.group(1)
        n = int(m.group(2))
        if prefix not in _PREFIX_TO_CCXT_UNIT:
            raise ValueError(
                f"Unrecognized canonical prefix '{prefix}' in '{interval}'. "
                f"Supported: {list(_PREFIX_TO_CCXT_UNIT)}"
            )
        return n, prefix

    raise ValueError(
        f"Cannot parse timeframe '{interval}'. "
        "Expected ccxt format (e.g. '1h', '30m', '1M') "
        "or canonical label (e.g. 'H1', 'M30', 'MN1')."
    )


def to_ccxt(timeframe: str) -> str:
    """Convert any timeframe string to ccxt format.

    Examples: ``'H1'`` → ``'1h'``, ``'M30'`` → ``'30m'``, ``'1h'`` → ``'1h'``
    """
    n, prefix = _parse_timeframe(timeframe)
    return f"{n}{_PREFIX_TO_CCXT_UNIT[prefix]}"


def to_canonical(timeframe: str) -> str:
    """Convert any timeframe string to canonical label.

    Examples: ``'1h'`` → ``'H1'``, ``'30m'`` → ``'M30'``, ``'H1'`` → ``'H1'``
    """
    n, prefix = _parse_timeframe(timeframe)
    return f"{prefix}{n}"


def interval_to_timedelta(interval: str) -> pd.Timedelta:
    """Convert any timeframe string to ``pd.Timedelta``.

    Accepts both ccxt (``'1h'``) and canonical (``'H1'``) formats.
    Any valid interval (``'M30'``, ``'H6'``, ``'H12'``, ``'D3'`` …) is supported.
    """
    n, prefix = _parse_timeframe(interval)
    fn = _PREFIX_TO_TIMEDELTA.get(prefix)
    if fn is None:
        raise ValueError(f"No timedelta mapping for prefix '{prefix}'")
    return fn(n)


_MIN_BARS_FOR_INFERENCE = 5
_HOUR_MIN = 60
_DAY_MIN = 24 * _HOUR_MIN
_WEEK_MIN = 7 * _DAY_MIN


def infer_timeframe(index: pd.DatetimeIndex) -> str:
    """Infer canonical timeframe label from a DatetimeIndex.

    Uses the mode of bar-to-bar timedeltas (robust to occasional gaps).
    Works for any bar size that is a whole number of minutes; does not
    require pre-registration of the timeframe.

    Returns canonical label: ``'M1'``, ``'M5'``, ``'M30'``, ``'H1'``,
    ``'H4'``, ``'D1'``, ``'W1'``, ``'MN1'`` etc.

    Raises
    ------
    ValueError
        If fewer than 5 bars are provided or the mode timedelta is
        non-positive or sub-minute.
    """
    if len(index) < _MIN_BARS_FOR_INFERENCE:
        raise ValueError(
            f"Cannot infer timeframe from {len(index)} bars "
            f"(minimum {_MIN_BARS_FOR_INFERENCE} required)"
        )
    diffs = pd.Series(index).diff().dropna()
    mode_td = diffs.mode().iloc[0]

    total_secs = mode_td.total_seconds()
    if total_secs <= 0:
        raise ValueError(f"Non-positive bar timedelta {mode_td}; cannot infer timeframe")

    total_minutes = total_secs / 60

    # Monthly: 25–32 days (handles variable month lengths)
    if _DAY_MIN * 25 <= total_minutes <= _DAY_MIN * 32:
        return "MN1"
    if total_minutes >= _WEEK_MIN and total_minutes % _WEEK_MIN == 0:
        return f"W{int(total_minutes // _WEEK_MIN)}"
    if total_minutes >= _DAY_MIN and total_minutes % _DAY_MIN == 0:
        return f"D{int(total_minutes // _DAY_MIN)}"
    if total_minutes >= _HOUR_MIN and total_minutes % _HOUR_MIN == 0:
        return f"H{int(total_minutes // _HOUR_MIN)}"

    n_min = round(total_minutes)
    if n_min < 1:
        raise ValueError(f"Bar interval {mode_td} is sub-minute; not supported by canonical labels")
    return f"M{n_min}"
