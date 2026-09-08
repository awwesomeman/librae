"""IBKRAdapter — Interactive Brokers adapter for US equities and futures.

Wraps ``ib_async`` (community-maintained continuation of the archived
``ib_insync``) using the same flat, duck-typed adapter style as
ShioajiAdapter/CryptoAdapter.

Stocks are SMART-routed by symbol alone (IBKR resolves the exchange).
Futures aren't — pass ``security_type="FUT"`` plus the contract's listing
``exchange`` (e.g. ``"CME"`` for ES/NQ, ``"NYMEX"`` for CL, ``"COMEX"`` for
GC). Exact contracts use ``contract_month="YYYYMM"``; a deliberately dynamic
front-month route uses ``continuous_alias=True``. It doesn't back-adjust a
continuous price series itself — that's a data-layer concern.

Unlike Shioaji (login+CA) or CCXT (API key), IBKR authenticates at the
TWS/IB Gateway process, not per-adapter — the adapter just opens a socket
connection to an already-running, already-logged-in gateway. Paper vs live
is which port that gateway listens on (7497/4002 = paper, 7496/4001 = live),
not something this adapter chooses.

Credentials can be passed explicitly or loaded from environment variables
using the ``IBKR_`` prefix convention::

    IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID

Install: ``pip install ib-async`` or ``pip install -e '.[us-live]'``
"""

from __future__ import annotations

import logging
from calendar import monthrange
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isclose, isfinite
from threading import Lock, RLock

import pandas as pd

from librae.config.symbols import (
    AssetClass,
    AvailableSymbol,
    InstrumentKind,
    canonicalize_price_to_increment,
)
from librae.core.run_config import MarketDataSessionMode
from librae.core.trading_calendar import (
    next_session_open,
    session_bounds,
    session_lookback_days,
    validate_calendar_id,
)
from librae.core.utils import floor_to_step, validate_contract_month
from librae.live.executor import PositionRequest

from .base import (
    AdapterInfo,
    CredentialConfig,
    drop_incomplete_ohlcv,
    find_position,
    passive_price,
    validate_order_signal,
)

logger = logging.getLogger(__name__)

# ccxt-style timeframe -> IBKR barSizeSetting string.
_BAR_SIZE_MAP = {
    "1m": "1 min",
    "2m": "2 mins",
    "3m": "3 mins",
    "5m": "5 mins",
    "10m": "10 mins",
    "15m": "15 mins",
    "20m": "20 mins",
    "30m": "30 mins",
    "1h": "1 hour",
    "2h": "2 hours",
    "3h": "3 hours",
    "4h": "4 hours",
    "8h": "8 hours",
    "1d": "1 day",
    "1w": "1 week",
    "1M": "1 month",
}
_NATIVE_CALENDAR_TIMEFRAMES = frozenset({"1d", "1w", "1M"})
_MARKET_RULE_CACHE_MAXSIZE = 128
_MarketRuleLadder = tuple[tuple[float, float], ...]


def _utc_today() -> date:
    """Return the UTC date used for futures-expiry decisions."""
    return datetime.now(UTC).date()


def _utc_now() -> datetime:
    """Return the current UTC time used by daily-bar availability checks."""
    return datetime.now(UTC)


def _resolve_session_mode(
    session_mode: MarketDataSessionMode | None,
    use_rth: bool | None,
) -> MarketDataSessionMode:
    """Resolve the generic session contract and legacy IBKR spelling."""
    if session_mode is not None and session_mode not in ("regular", "extended"):
        raise ValueError(f"session_mode must be 'regular' or 'extended', got {session_mode!r}")
    if use_rth is not None and not isinstance(use_rth, bool):
        raise TypeError("use_rth must be a bool or None")
    legacy_mode: MarketDataSessionMode | None = None
    if use_rth is not None:
        legacy_mode = "regular" if use_rth else "extended"
    if session_mode is not None and legacy_mode is not None and session_mode != legacy_mode:
        raise ValueError("session_mode and use_rth request different IBKR sessions")
    return session_mode or legacy_mode or "extended"


def _ibkr_session_date(value: object) -> date:
    """Parse IBKR's date-only daily-bar label without treating it as UTC."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip()
    try:
        parsed = pd.to_datetime(raw, format="%Y%m%d", errors="raise")
    except (TypeError, ValueError):
        try:
            parsed = pd.to_datetime(raw, format="%Y-%m-%d", errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid IBKR daily session date: {value!r}") from exc
    return pd.Timestamp(parsed).date()


def _normalize_daily_bars(
    frame: pd.DataFrame,
    *,
    calendar_id: str,
    session_mode: MarketDataSessionMode,
) -> pd.DataFrame:
    """Anchor IBKR date labels and attach a conservative usability bound."""
    session_dates = frame["date"].map(_ibkr_session_date)
    bounds = [session_bounds(label, calendar_id) for label in session_dates]
    frame = frame.copy()
    frame["session_date"] = session_dates
    frame["ts"] = pd.DatetimeIndex([opened_at for opened_at, _ in bounds])
    if session_mode == "regular":
        available_at = [closed_at for _, closed_at in bounds]
    else:
        # The generic exchange calendar owns regular-session geometry, not
        # vendor-specific pre/post-market hours. Waiting until the following
        # session opens is conservative and prevents a partial extended-hours
        # daily value from entering a feature early. This is an eligibility
        # policy, not an assertion about IBKR's actual publication timestamp.
        available_at = [next_session_open(label, calendar_id) for label in session_dates]
    frame["available_at"] = pd.DatetimeIndex(available_at)
    return frame


def _contract_expiry_date(contract: object) -> date:
    """Parse IBKR's YYYYMM or YYYYMMDD contract expiry."""
    raw_expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "")).strip()
    date_token = raw_expiry.split(maxsplit=1)[0]
    if len(date_token) >= 8 and date_token[:8].isdigit():
        year, month, day = (
            int(date_token[:4]),
            int(date_token[4:6]),
            int(date_token[6:8]),
        )
    elif len(date_token) >= 6 and date_token[:6].isdigit():
        year, month = int(date_token[:4]), int(date_token[4:6])
        try:
            day = monthrange(year, month)[1]
        except (ValueError, IndexError) as exc:
            raise ValueError(f"Invalid IBKR contract expiry: {raw_expiry!r}") from exc
    else:
        raise ValueError(f"Invalid IBKR contract expiry: {raw_expiry!r}")
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"Invalid IBKR contract expiry: {raw_expiry!r}") from exc


def _future_contract_is_current(contract: object) -> bool:
    """Return whether a cached futures contract has not expired."""
    return _contract_expiry_date(contract) >= _utc_today()


def _require_ib_async():
    """Import and return ib_async, raising a friendly error if missing."""
    try:
        import ib_async

        return ib_async
    except ImportError as e:
        raise ImportError(
            "IBKRAdapter requires the optional 'us-live' dependencies. "
            "From a repository clone run: uv sync --extra us-live. "
            "For a direct install, include Librae's 'us-live' extra."
        ) from e


def _to_bar_size(timeframe: str) -> str:
    if timeframe not in _BAR_SIZE_MAP:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r} for IBKRAdapter. "
            f"Supported: {sorted(_BAR_SIZE_MAP)}"
        )
    return _BAR_SIZE_MAP[timeframe]


def _default_duration_str(timeframe: str, limit: int) -> str:
    """Approximate durationStr covering `limit` bars when no start/end is
    given — IBKR wants a duration, not a bar count, for reqHistoricalData."""
    from librae.core.utils import interval_to_timedelta

    total_seconds = interval_to_timedelta(timeframe).total_seconds() * limit
    days = max(1, int(-(-total_seconds // 86400)))  # ceil division
    return f"{days} D"


@dataclass
class IBKRCredentials(CredentialConfig):
    """Connection info for a running TWS/IB Gateway instance.

    Not API-key credentials — IBKR authenticates the human at the gateway
    login screen; this only says which socket to dial. Port convention:
    7497 TWS paper, 7496 TWS live, 4002 IB Gateway paper, 4001 IB Gateway live.
    """

    host: str = "127.0.0.1"
    port: str = "7497"
    client_id: str = "1"


class IBKRAdapter:
    """US equities adapter backed by Interactive Brokers via ib_async.

    Parameters
    ----------
    credentials : IBKRCredentials | None
        If None, loads from env vars with ``IBKR_`` prefix.
    trading_enabled : bool
        If False (default), connects with IBKR's own ``readonly`` socket
        flag — ``place_order``/``get_position`` raise ``NotImplementedError``
        without ever reaching the gateway's order-entry path. Mirrors
        ShioajiAdapter's CA-gated read-only default and CryptoAdapter's
        no-api-key read-only default: safe unless explicitly opted in.

    The adapter connects during ``__init__``. Call ``close()`` or use as a
    context manager to disconnect.

    Order preparation and submission are a single-threaded adapter contract:
    call them on the thread/event loop that owns ``ib_async``. The market-rule
    single-flight lock only coalesces cache misses; it does not marshal SDK
    calls between threads or event loops.
    """

    def __init__(
        self,
        credentials: IBKRCredentials | None = None,
        trading_enabled: bool = False,
    ) -> None:
        ib_async = _require_ib_async()
        creds = credentials or IBKRCredentials.from_env("IBKR")

        self._ib = ib_async.IB()
        self._read_only = not trading_enabled
        self._contract_cache: dict[tuple[str, str, str | None, str, str | None], object] = {}
        self._contract_details_cache: dict[
            tuple[str, str, str | None, str, str | None], object
        ] = {}
        self._market_rule_cache: OrderedDict[int, _MarketRuleLadder] = OrderedDict()
        self._connection_state_lock = RLock()
        self._market_rule_singleflight_lock = Lock()
        self._connection_generation = 0
        self._ib.connectedEvent += self._on_connection_boundary
        self._ib.disconnectedEvent += self._on_connection_boundary
        self._ib.connect(
            creds.host,
            int(creds.port),
            clientId=int(creds.client_id),
            readonly=self._read_only,
        )
        logger.info(
            "IBKR connected host=%s port=%s clientId=%s trading_enabled=%s",
            creds.host,
            creds.port,
            creds.client_id,
            trading_enabled,
        )

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="ibkr",
            venue="IBKR",
            market_type="us_equity",
        )

    def available_symbols(
        self,
        *,
        query: str,
        kind: InstrumentKind,
        asset_class: AssetClass | None = None,
        exchange: str | None = None,
        currency: str = "USD",
    ) -> tuple[AvailableSymbol, ...]:
        """Discover one stock or an unexpired IBKR futures chain."""
        symbol = query.strip().upper()
        if not symbol:
            raise ValueError("IBKR available_symbols requires a symbol/root query")
        if kind == "perpetual":
            return ()
        ib_async = _require_ib_async()
        if kind == "spot":
            contract = ib_async.Stock(symbol, "SMART", currency)
        else:
            if not exchange:
                raise ValueError("IBKR futures discovery requires exchange")
            contract = ib_async.Future(symbol, exchange=exchange, currency=currency)
        details = list(self._ib.reqContractDetails(contract))
        if not details:
            raise ValueError(f"No IBKR {kind} contracts found for {symbol}")

        if kind == "spot":
            if asset_class not in (None, "equity"):
                return ()
            results = []
            seen_contract_ids: set[int] = set()
            for detail in details:
                resolved = detail.contract
                contract_id = int(getattr(resolved, "conId", 0) or 0)
                if contract_id and contract_id in seen_contract_ids:
                    continue
                seen_contract_ids.add(contract_id)
                native_symbol = str(
                    getattr(resolved, "localSymbol", "") or getattr(resolved, "symbol", "")
                )
                results.append(
                    AvailableSymbol(
                        broker="ibkr",
                        canonical_symbol=symbol,
                        venue_symbol=symbol,
                        native_symbol=native_symbol,
                        name=str(getattr(detail, "longName", "") or native_symbol),
                        kind="spot",
                        asset_class="equity",
                        currency=str(getattr(resolved, "currency", "") or currency),
                        instrument_type="spot",
                        security_type="STK",
                        exchange="SMART",
                        multiplier=1.0,
                        tick_size=self._positive_float(getattr(detail, "minTick", None)),
                    )
                )
            return tuple(sorted(results, key=lambda item: item.native_symbol))

        unexpired: list[tuple[date, str, object]] = []
        for detail in details:
            resolved = detail.contract
            expiry = _contract_expiry_date(resolved)
            if expiry >= _utc_today():
                raw_expiry = str(getattr(resolved, "lastTradeDateOrContractMonth", "")).strip()
                unexpired.append((expiry, raw_expiry, detail))
        if not unexpired:
            raise ValueError(f"No non-expired IBKR futures found for {symbol}")
        unexpired.sort(
            key=lambda item: (
                item[0],
                str(getattr(item[2].contract, "localSymbol", "")),
            )
        )
        expiry_ranks = {
            expiry: rank for rank, expiry in enumerate(sorted({item[0] for item in unexpired}))
        }
        month_counts: dict[str, int] = {}
        for _, raw_expiry, _ in unexpired:
            month = raw_expiry[:6]
            month_counts[month] = month_counts.get(month, 0) + 1

        results = []
        for expiry, raw_expiry, detail in unexpired:
            resolved = detail.contract
            contract_month = validate_contract_month(raw_expiry[:6])
            descriptor = " ".join(
                str(getattr(detail, field, "") or "")
                for field in ("category", "subcategory", "longName")
            ).upper()
            if "INDEX" in descriptor:
                resolved_asset_class: AssetClass = "index"
            elif any(token in descriptor for token in ("METAL", "ENERGY", "AGRICULT", "COMMOD")):
                resolved_asset_class = "commodity"
            elif any(token in descriptor for token in ("INTEREST", "RATE", "BOND")):
                resolved_asset_class = "rate"
            elif any(token in descriptor for token in ("FOREX", "CURRENCY", " FX ")):
                resolved_asset_class = "fx"
            else:
                resolved_asset_class = "unknown"
            if asset_class is not None and asset_class != resolved_asset_class:
                continue
            native_symbol = str(getattr(resolved, "localSymbol", "") or symbol)
            canonical_suffix = (
                raw_expiry[:8] if month_counts[contract_month] > 1 else contract_month
            )
            raw_multiplier = getattr(resolved, "multiplier", None)
            results.append(
                AvailableSymbol(
                    broker="ibkr",
                    canonical_symbol=f"{symbol}_{canonical_suffix}",
                    venue_symbol=symbol,
                    native_symbol=native_symbol,
                    name=str(getattr(detail, "longName", "") or native_symbol),
                    kind="future",
                    asset_class=resolved_asset_class,
                    currency=str(getattr(resolved, "currency", "") or currency),
                    instrument_type=(
                        "contract_quarterly"
                        if contract_month[4:] in ("03", "06", "09", "12")
                        else "contract_monthly"
                    ),
                    security_type="FUT",
                    exchange=str(getattr(resolved, "exchange", "") or exchange),
                    contract_month=contract_month,
                    delivery_month=contract_month,
                    contract_rank=expiry_ranks[expiry],
                    multiplier=(
                        float(raw_multiplier) if raw_multiplier not in (None, "") else None
                    ),
                    tick_size=self._positive_float(getattr(detail, "minTick", None)),
                )
            )
        return tuple(results)

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
        security_type: str = "STK",
        exchange: str | None = None,
        currency: str = "USD",
        continuous_alias: bool = False,
        contract_month: str | None = None,
        calendar_id: str | None = None,
        session_mode: MarketDataSessionMode | None = None,
        use_rth: bool | None = None,
        drop_incomplete: bool = False,
    ) -> pd.DataFrame:
        """Fetch OHLCV via IBKR's reqHistoricalData.

        Args:
            symbol: Stock ticker (e.g. ``"MU"``) or futures root (e.g. ``"ES"``).
            timeframe: ccxt-format candle interval — one of
                ``_BAR_SIZE_MAP`` (e.g. ``"1m"``, ``"1h"``, ``"1d"``).
            start/end: Date range as datetime or ``"YYYY-MM-DD"`` string.
                If omitted, fetches the most recent *limit* bars.
            limit: Max bars (used only when start/end are omitted).
            security_type: ``"STK"`` (default) or ``"FUT"``.
            exchange: Required when security_type="FUT" (e.g. ``"CME"``,
                ``"NYMEX"``, ``"COMEX"``) — futures aren't SMART-routed.
                Ignored for stocks (always routed via SMART).
            currency: Contract currency, default ``"USD"``.
            continuous_alias: For futures only, explicitly resolve the nearest
                non-expired contract.
            contract_month: For futures only, exact expiry month in ``YYYYMM``
                form. Mutually exclusive with ``continuous_alias``.
            calendar_id: Exchange calendar used to anchor IBKR's date-only
                daily labels. Required for ``1d`` requests.
            session_mode: ``"extended"`` (default) requests every session
                exposed by IBKR; ``"regular"`` requests RTH only.
            use_rth: Adapter-specific compatibility spelling retained for direct
                callers. It must agree with ``session_mode`` when both are set.
            drop_incomplete: Drop the current still-forming candle.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is the UTC-aware bar-start datetime. Daily stock bars
        additionally include ``session_date`` and ``available_at``. Native
        weekly/monthly bars and calendar-sized futures bars are rejected:
        build them from completed, normalized lower-frequency data instead.

        IBKR's own pacing/lookback limits per bar size (e.g. 1-sec bars only
        go back a few days) apply and aren't paginated around here; a
        window too long for the requested bar size raises from ib_async
        directly.
        """
        bar_size = _to_bar_size(timeframe)
        resolved_session_mode = _resolve_session_mode(session_mode, use_rth)
        if security_type == "FUT" and timeframe in _NATIVE_CALENDAR_TIMEFRAMES:
            raise NotImplementedError(
                "IBKR native calendar-sized futures bars may use settlement values "
                "published or revised later; fetch completed intraday bars and "
                "resample them by calendar session"
            )
        if timeframe in {"1w", "1M"}:
            raise NotImplementedError(
                "IBKR native weekly/monthly bars use date labels without a safe "
                "availability instant; fetch normalized daily or intraday bars and "
                "aggregate them by calendar period"
            )
        if timeframe == "1d" and calendar_id is None:
            raise ValueError("IBKR 1d bars require calendar_id for session-date normalization")
        if timeframe == "1d":
            validate_calendar_id(calendar_id)

        contract = self._resolve_contract(
            symbol,
            security_type=security_type,
            exchange=exchange,
            currency=currency,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
        )

        requested_at = _utc_now()
        end_dt = _parse_dt(end) if end else requested_at
        if start:
            start_dt = _parse_dt(start)
            duration = f"{max(1, (end_dt - start_dt).days + 1)} D"
        elif timeframe == "1d":
            if calendar_id is None:  # guarded before duration calculation
                raise RuntimeError("calendar_id unexpectedly missing for IBKR daily bars")
            # Include one possible current/forming session. The adapter later
            # applies source availability and tails the requested row count.
            duration = f"{session_lookback_days(end_dt, limit + 1, calendar_id)} D"
        else:
            duration = _default_duration_str(timeframe, limit)

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=resolved_session_mode == "regular",
            # IBKR ignores epoch mode for day bars and returns yyyyMMdd.
            formatDate=1 if timeframe == "1d" else 2,
        )

        ib_async = _require_ib_async()
        df = ib_async.util.df(bars)
        if df is None or df.empty:
            columns = ["ts", "open", "high", "low", "close", "volume"]
            if timeframe == "1d":
                columns.extend(["session_date", "available_at"])
            return pd.DataFrame(columns=columns)

        if timeframe == "1d":
            if calendar_id is None:  # guarded before the request
                raise RuntimeError("calendar_id unexpectedly missing for IBKR daily bars")
            df = _normalize_daily_bars(
                df,
                calendar_id=calendar_id,
                session_mode=resolved_session_mode,
            )
            df = df[
                [
                    "ts",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "session_date",
                    "available_at",
                ]
            ]
            df = df[df["available_at"] <= pd.Timestamp(requested_at)]
        else:
            df = df.rename(columns={"date": "ts"})
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            df = df[["ts", "open", "high", "low", "close", "volume"]]

        if start:
            if timeframe == "1d":
                df = df[
                    (df["session_date"] >= start_dt.date()) & (df["session_date"] <= end_dt.date())
                ]
            else:
                df = df[(df["ts"] >= start_dt) & (df["ts"] <= end_dt)]
        elif len(df) > limit:
            df = df.tail(limit)
        if drop_incomplete and timeframe != "1d":
            # Extended bars may be outside the regular-session geometry held
            # by exchange_calendars (for example XNYS pre/post-market). Fixed
            # interval completion is safer than misclassifying them as invalid.
            completion_calendar = calendar_id if resolved_session_mode == "regular" else None
            df = drop_incomplete_ohlcv(
                df,
                timeframe,
                calendar_id=completion_calendar,
            )
        return df.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Order management (requires trading_enabled=True)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "IBKRAdapter connected read-only — pass trading_enabled=True "
                "to enable order placement."
            )

    @staticmethod
    def _routing_exchange(signal: dict) -> str:
        if signal["security_type"] == "STK":
            return "SMART"
        exchange = str(signal.get("exchange") or "").strip().upper()
        if not exchange:
            raise ValueError(f"{signal['symbol']} has no IBKR order-routing exchange")
        return exchange

    @staticmethod
    def _market_rule_id(details: object, routing_exchange: str, symbol: str) -> int | None:
        market_rule_ids = str(getattr(details, "marketRuleIds", "") or "").strip()
        if not market_rule_ids:
            return None
        valid_exchanges = str(getattr(details, "validExchanges", "") or "").strip()
        if not valid_exchanges:
            raise ValueError(f"{symbol} has IBKR marketRuleIds without validExchanges")
        exchanges = [item.strip().upper() for item in valid_exchanges.split(",")]
        rule_ids = [item.strip() for item in market_rule_ids.split(",")]
        if len(exchanges) != len(rule_ids) or any(not item for item in exchanges):
            raise ValueError(f"{symbol} has malformed IBKR exchange/market-rule mapping")
        matching = [
            index for index, exchange in enumerate(exchanges) if exchange == routing_exchange
        ]
        if not matching:
            raise ValueError(
                f"{symbol} has no IBKR market rule for routing exchange {routing_exchange}"
            )
        if len(matching) != 1:
            raise ValueError(
                f"{symbol} has ambiguous IBKR market rules for routing exchange {routing_exchange}"
            )
        raw_rule_id = rule_ids[matching[0]]
        if not raw_rule_id.isdigit() or int(raw_rule_id) <= 0:
            raise ValueError(
                f"{symbol} has invalid IBKR market rule for routing exchange {routing_exchange}"
            )
        return int(raw_rule_id)

    @classmethod
    def _parse_market_rule_ladder(cls, raw_ladder: object, rule_id: int) -> _MarketRuleLadder:
        if raw_ladder is None:
            raise ValueError(f"IBKR market rule {rule_id} request timed out")
        try:
            increments = list(raw_ladder)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"IBKR market rule {rule_id} returned a malformed ladder") from exc
        if not increments:
            raise ValueError(f"IBKR market rule {rule_id} returned no price increments")

        ladder: list[tuple[float, float]] = []
        for item in increments:
            low_edge = cls._optional_float(getattr(item, "lowEdge", None))
            increment = cls._positive_float(getattr(item, "increment", None))
            if low_edge is None or low_edge < 0 or increment is None:
                raise ValueError(f"IBKR market rule {rule_id} returned a malformed ladder")
            if ladder and low_edge <= ladder[-1][0]:
                raise ValueError(f"IBKR market rule {rule_id} returned a malformed ladder")
            ladder.append((low_edge, increment))
        if ladder[0][0] != 0:
            raise ValueError(f"IBKR market rule {rule_id} does not cover positive prices")
        return tuple(ladder)

    def _market_rule_ladder(
        self,
        rule_id: int,
        *,
        expected_generation: int,
    ) -> _MarketRuleLadder:
        with self._market_rule_singleflight_lock:
            with self._connection_state_lock:
                if self._connection_generation != expected_generation:
                    raise ValueError("IBKR connection changed while resolving the market rule")
                cached = self._market_rule_cache.get(rule_id)
                if cached is not None:
                    self._market_rule_cache.move_to_end(rule_id)
                    return cached

            ladder = self._parse_market_rule_ladder(self._ib.reqMarketRule(rule_id), rule_id)

            with self._connection_state_lock:
                if self._connection_generation != expected_generation:
                    raise ValueError("IBKR market-rule response is stale after a connection change")
                self._market_rule_cache[rule_id] = ladder
                self._market_rule_cache.move_to_end(rule_id)
                while len(self._market_rule_cache) > _MARKET_RULE_CACHE_MAXSIZE:
                    self._market_rule_cache.popitem(last=False)
                return ladder

    @staticmethod
    def _increment_at(price: float, ladder: _MarketRuleLadder) -> float:
        for low_edge, increment in reversed(ladder):
            if price >= low_edge:
                return increment
        raise ValueError("IBKR market-rule ladder does not cover the submitted price")

    @classmethod
    def _normalize_to_market_rule(
        cls,
        price: float,
        side: str,
        ladder: _MarketRuleLadder,
    ) -> float:
        normalized = price
        for _ in range(len(ladder) + 2):
            increment = cls._increment_at(normalized, ladder)
            next_price = passive_price(normalized, increment, side)
            if not isfinite(next_price) or next_price <= 0:
                raise ValueError("IBKR market-rule normalization produced a non-positive price")
            if next_price == normalized:
                return normalized
            normalized = next_price
        raise ValueError("IBKR market-rule normalization did not reach a stable price band")

    def _normalize_limit_price(
        self,
        signal: dict,
        details: object,
        *,
        expected_generation: int,
    ) -> float:
        routing_exchange = self._routing_exchange(signal)
        rule_id = self._market_rule_id(details, routing_exchange, signal["symbol"])
        if rule_id is not None:
            ladder = self._market_rule_ladder(
                rule_id,
                expected_generation=expected_generation,
            )
            normalized = self._normalize_to_market_rule(
                float(signal["price"]), signal["side"], ladder
            )
            with self._connection_state_lock:
                if self._connection_generation != expected_generation:
                    raise ValueError("IBKR market-rule result is stale after a connection change")
            return normalized

        tick_size = self._positive_float(getattr(details, "minTick", None))
        if tick_size is None:
            raise ValueError(f"{signal['symbol']} has no positive IBKR minTick")
        normalized = passive_price(float(signal["price"]), tick_size, signal["side"])
        with self._connection_state_lock:
            if self._connection_generation != expected_generation:
                raise ValueError("IBKR contract details are stale after a connection change")
        return normalized

    def normalize_limit_price(self, signal: dict) -> float:
        """Normalize a limit price using the routed contract's IBKR price grid."""
        validate_order_signal(signal)
        if signal.get("order_type") != "limit":
            raise ValueError("normalize_limit_price requires a limit order")
        with self._connection_state_lock:
            generation = self._connection_generation
        details = self._contract_details(
            signal["symbol"],
            security_type=signal["security_type"],
            exchange=signal.get("exchange"),
            currency=signal["currency"],
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
            expected_generation=generation,
        )
        return self._normalize_limit_price(
            signal,
            details,
            expected_generation=generation,
        )

    def prepare_order(self, signal: dict) -> dict:
        """Apply IBKR ContractDetails size and tick constraints."""
        validate_order_signal(signal)
        with self._connection_state_lock:
            generation = self._connection_generation
        details = self._contract_details(
            signal["symbol"],
            security_type=signal["security_type"],
            exchange=signal.get("exchange"),
            currency=signal["currency"],
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
            expected_generation=generation,
        )
        step = (
            self._positive_float(getattr(details, "sizeIncrement", None))
            or self._positive_float(getattr(details, "suggestedSizeIncrement", None))
            or 1.0
        )
        minimum = self._positive_float(getattr(details, "minSize", None)) or step
        quantity = floor_to_step(float(signal["quantity"]), step)
        if quantity < minimum:
            raise ValueError(f"{signal['symbol']} quantity {quantity} is below minimum {minimum}")

        prepared = dict(signal)
        prepared["quantity"] = quantity
        if signal.get("order_type") == "limit":
            raw_increment = signal.get("price_increment")
            if raw_increment is not None:
                prepared["price"] = canonicalize_price_to_increment(
                    float(signal["price"]),
                    raw_increment,
                    context=f"{signal['symbol']} limit price",
                )
            else:
                prepared["price"] = self._normalize_limit_price(
                    signal,
                    details,
                    expected_generation=generation,
                )
        return prepared

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side`` (``"buy"``/``"sell"``),
        ``quantity``, ``order_type`` (``"market"``/``"limit"``), ``time_in_force``
        (``"day"``/``"gtc"``/``"ioc"``/``"fok"``, set as IBKR's ``order.tif``),
        optionally ``price`` for limit orders, plus explicit ``security_type``
        (``"STK"`` or ``"FUT"``), ``exchange`` (required for ``"FUT"``),
        and ``currency``. It also accepts an optional
        ``client_order_id`` (set as IBKR's ``orderRef`` — an audit-trail tag,
        not an enforced dedup key; check ``self._ib.openTrades()`` for a
        matching ``orderRef`` before resubmitting if that matters to the caller).
        """
        self._require_auth()
        validate_order_signal(signal)
        ib_async = _require_ib_async()

        contract = self._resolve_contract(
            signal["symbol"],
            security_type=signal["security_type"],
            exchange=signal.get("exchange"),
            currency=signal["currency"],
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        action = "BUY" if signal["side"] == "buy" else "SELL"
        if signal["order_type"] == "limit":
            order = ib_async.LimitOrder(action, signal["quantity"], signal["price"])
        else:
            order = ib_async.MarketOrder(action, signal["quantity"])
        order.tif = {"day": "DAY", "gtc": "GTC", "ioc": "IOC", "fok": "FOK"}[
            signal["time_in_force"]
        ]
        if signal.get("client_order_id"):
            order.orderRef = signal["client_order_id"]

        trade = self._ib.placeOrder(contract, order)
        self._ib.sleep(0)  # pump the event loop so orderStatus reflects the ack
        return self._trade_to_order(trade)

    def find_order(self, client_order_id: str, symbol: str) -> dict | None:
        """Find a current or completed order by IBKR ``orderRef``."""
        self._require_auth()
        matches = [
            trade
            for trade in self._ib.trades()
            if trade.contract.symbol == symbol and trade.order.orderRef == client_order_id
        ]
        if not matches:
            matches = [
                trade
                for trade in self._load_completed_trades()
                if trade.contract.symbol == symbol and trade.order.orderRef == client_order_id
            ]
        if len(matches) > 1:
            raise ValueError(f"duplicate IBKR orderRef: {client_order_id}")
        return self._trade_to_order(matches[0]) if matches else None

    def get_order(self, order_id: str, symbol: str) -> dict:
        """Return the latest cumulative state, including a prior session."""
        self._require_auth()
        for trade in self._ib.trades():
            if str(trade.order.orderId) == order_id and trade.contract.symbol == symbol:
                return self._trade_to_order(trade)
        for trade in self._load_completed_trades():
            if str(trade.order.orderId) == order_id and trade.contract.symbol == symbol:
                return self._trade_to_order(trade)
        raise LookupError(f"IBKR order not found: {order_id}")

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Return open trades maintained by the connected IBKR client."""
        self._require_auth()
        return [
            self._trade_to_order(trade)
            for trade in self._ib.openTrades()
            if trade.contract.symbol == symbol
        ]

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open IBKR trade and return its refreshed state."""
        self._require_auth()
        for trade in self._ib.openTrades():
            if str(trade.order.orderId) == order_id and trade.contract.symbol == symbol:
                self._ib.cancelOrder(trade.order)
                self._ib.sleep(0)
                return self._trade_to_order(trade)
        return self.get_order(order_id, symbol)

    def _load_completed_trades(self) -> list:
        """Load completed orders and enrich them with execution details.

        ib_async's completed-order response has status but no fills. Executions
        supply the quantity, average price, time, and commission required by
        the engine's cumulative report contract.
        """
        trades = list(self._ib.reqCompletedOrders(apiOnly=True))
        fills = list(self._ib.reqExecutions())
        for trade in trades:
            order_id = trade.order.orderId
            perm_id = getattr(trade.order, "permId", 0)
            trade.fills = [
                fill
                for fill in fills
                if fill.execution.orderId == order_id
                or (perm_id and fill.execution.permId == perm_id)
            ]
            quantities = [self._optional_float(fill.execution.shares) for fill in trade.fills]
            if trade.fills and all(quantity is not None for quantity in quantities):
                filled = sum(quantity for quantity in quantities if quantity is not None)
                if filled > 0:
                    notional = sum(
                        float(quantity) * float(fill.execution.price)
                        for fill, quantity in zip(trade.fills, quantities, strict=True)
                    )
                    trade.orderStatus.filled = filled
                    trade.orderStatus.avgFillPrice = notional / filled
        return trades

    @staticmethod
    def _trade_to_order(trade) -> dict:
        """Translate an ib_async Trade to the engine's cumulative contract."""
        fills = list(trade.fills)
        filled = IBKRAdapter._optional_float(trade.orderStatus.filled)
        average = IBKRAdapter._optional_float(trade.orderStatus.avgFillPrice)
        commissions = []
        for fill in fills:
            report = getattr(fill, "commissionReport", None)
            commission = IBKRAdapter._optional_float(getattr(report, "commission", None))
            if commission is not None:
                commissions.append(commission)
        result = {
            "id": str(trade.order.orderId),
            "status": trade.orderStatus.status,
        }
        client_order_id = trade.order.orderRef
        symbol = trade.contract.symbol
        side = trade.order.action
        requested = IBKRAdapter._optional_float(trade.order.totalQuantity)
        if isinstance(client_order_id, str) and client_order_id:
            result["clientOrderId"] = client_order_id
        if isinstance(symbol, str):
            result["symbol"] = symbol
        if isinstance(side, str):
            result["side"] = side.lower()
        if requested is not None:
            result["amount"] = requested
        if filled is not None:
            result["filled"] = filled
        if average is not None and average > 0:
            result["average"] = average
        if filled is not None and filled > 0:
            if len(commissions) == len(fills):
                result["commission"] = sum(commissions)
            if fills:
                result["executed_at"] = max(fill.time for fill in fills)
        return result

    @staticmethod
    def _optional_float(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if isfinite(number) and abs(number) < 1e307 else None

    def get_position(
        self,
        request: PositionRequest,
    ) -> dict:
        """Return the current position for one configured instrument.

        ``request.multiplier`` is the engine accounting SSOT and must match
        the resolved broker contract. ``unrealized_pnl`` is always 0.0 —
        IBKR's ``positions()`` doesn't carry live uPnL (that needs a separate
        ``reqPnLSingle`` subscription per symbol); not implemented here,
        matching the scope of the other adapters' position snapshot.
        """
        self._require_auth()
        symbol = request.venue_symbol
        if request.security_type is None:
            raise ValueError(f"IBKR position request for {symbol} requires security_type")
        contract = self._resolve_contract(
            symbol,
            security_type=request.security_type,
            exchange=request.exchange,
            currency=request.currency,
            continuous_alias=request.continuous_alias,
            contract_month=request.contract_month,
        )
        contract_id = int(getattr(contract, "conId", 0) or 0)
        if contract_id <= 0:
            raise ValueError(f"IBKR returned no stable conId for {symbol}")
        raw_multiplier = getattr(contract, "multiplier", None)
        if request.security_type.upper() == "FUT" and raw_multiplier in (None, ""):
            raise ValueError(f"IBKR returned no contract multiplier for future {symbol}")
        contract_multiplier = float(raw_multiplier or 1.0)
        if not isfinite(contract_multiplier) or contract_multiplier <= 0:
            raise ValueError(f"IBKR returned invalid contract multiplier for {symbol}")
        if not isclose(
            contract_multiplier,
            request.multiplier,
            rel_tol=1e-9,
            abs_tol=1e-12,
        ):
            raise ValueError(
                f"IBKR contract multiplier mismatch for {symbol}: "
                f"broker={contract_multiplier}, configured={request.multiplier}"
            )
        return find_position(
            self._ib.positions(),
            request.symbol,
            matches=lambda p: int(getattr(p.contract, "conId", 0) or 0) == contract_id,
            size=lambda p: p.position,
            avg_price=lambda p: float(p.avgCost) / contract_multiplier,
        )

    def get_balance(self, currency: str) -> dict[str, float]:
        """Return account cash balance for *currency* via IBKR's accountSummary.

        UNVERIFIED against a live TWS/IB Gateway session — ``ib_async`` isn't
        installed in this dev environment and no live/paper account was
        available to confirm ``accountSummary()``'s tag semantics match what's
        assumed here (``TotalCashValue``, per-currency). Based on ib_async's
        public docs only; confirm against a real session before relying on
        this for cash-drift alerting (``LiveTrader._reconcile_cash``).
        """
        self._require_auth()
        values = self._ib.accountSummary()
        matches = [
            value
            for value in values
            if value.tag == "TotalCashValue" and value.currency == currency
        ]
        if not matches:
            raise ValueError(f"IBKR returned no TotalCashValue for {currency}")
        if len(matches) > 1:
            raise ValueError(
                f"IBKR returned ambiguous TotalCashValue values for {currency}: {len(matches)}"
            )
        total = float(matches[0].value)
        if not isfinite(total):
            raise ValueError(f"IBKR returned non-finite TotalCashValue for {currency}")
        return {"free": total, "used": 0.0, "total": total}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Disconnect from TWS/IB Gateway."""
        with self._connection_state_lock:
            generation = self._connection_generation
        self._ib.disconnect()
        with self._connection_state_lock:
            unchanged = self._connection_generation == generation
        if unchanged:
            self._on_connection_boundary()
        logger.info("IBKR disconnected")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_connection_boundary(self, *_: object) -> None:
        """Invalidate connection-scoped contracts and market-rule ladders."""
        with self._connection_state_lock:
            self._connection_generation += 1
            self._market_rule_cache.clear()
            self._contract_cache.clear()
            self._contract_details_cache.clear()

    def _resolve_contract(
        self,
        symbol: str,
        *,
        security_type: str = "STK",
        exchange: str | None = None,
        currency: str = "USD",
        continuous_alias: bool = False,
        contract_month: str | None = None,
        expected_generation: int | None = None,
    ):
        """Resolve a ticker/futures-root string to a qualified IBKR contract.
        Both qualifyContracts (stocks) and reqContractDetails (futures) hit
        IBKR (a blocking request/response round trip); cached per
        (symbol, security_type, exchange, currency, contract_month) so a live poll loop
        hitting the same contract repeatedly doesn't pay that round trip on
        every fetch/order call — mirrors ShioajiAdapter's contract lookup,
        which is already a cheap local dict lookup against contracts
        downloaded once at login.

        Stocks: SMART-routed by symbol alone, IBKR resolves the exchange.
        Futures: not SMART-routed — `exchange` is required (e.g. "CME" for
        ES/NQ, "NYMEX" for CL, "COMEX" for GC). Exactly one selection mode is
        required: ``contract_month="YYYYMM"`` for a dated contract, or
        ``continuous_alias=True`` for the nearest non-expired contract.
        """
        if security_type not in ("STK", "FUT"):
            raise ValueError(
                f"Unsupported security_type: {security_type!r} (expected 'STK' or 'FUT')"
            )
        if not isinstance(continuous_alias, bool):
            raise TypeError("continuous_alias must be a bool")
        validate_contract_month(contract_month)
        if security_type == "FUT":
            if not exchange:
                raise ValueError(
                    "exchange is required for security_type='FUT' (e.g. 'CME', "
                    "'NYMEX', 'COMEX') — futures aren't SMART-routed like stocks."
                )
            if continuous_alias == (contract_month is not None):
                raise ValueError(
                    "IBKR future requires exactly one of continuous_alias=True "
                    "or contract_month='YYYYMM'"
                )
        elif continuous_alias or contract_month is not None:
            raise ValueError("continuous_alias and contract_month are valid only for IBKR futures")

        cache_key = (symbol, security_type, exchange, currency, contract_month)
        with self._connection_state_lock:
            generation = self._connection_generation
            if expected_generation is not None and generation != expected_generation:
                raise ValueError("IBKR connection changed before resolving the contract")
            cached = self._contract_cache.get(cache_key)
            if cached is not None:
                if security_type != "FUT" or _future_contract_is_current(cached):
                    return cached
                self._contract_cache.pop(cache_key, None)
                self._contract_details_cache.pop(cache_key, None)

        ib_async = _require_ib_async()

        selected_detail = None
        if security_type == "STK":
            contract = ib_async.Stock(symbol, "SMART", currency)
            qualified = self._ib.qualifyContracts(contract)
            if not qualified:
                raise ValueError(f"Unknown symbol: {symbol}")
            resolved = qualified[0]
        else:
            future_kwargs = {"exchange": exchange, "currency": currency}
            if contract_month is not None:
                future_kwargs["lastTradeDateOrContractMonth"] = contract_month
            contract = ib_async.Future(symbol, **future_kwargs)
            details = list(self._ib.reqContractDetails(contract))
            if not details:
                selection = (
                    f" contract_month={contract_month}"
                    if contract_month is not None
                    else " continuous_alias=True"
                )
                raise ValueError(f"Unknown future: {symbol} on {exchange},{selection}")
            today = _utc_today()
            unexpired_details = []
            for detail in details:
                expiry = _contract_expiry_date(detail.contract)
                raw_expiry = str(
                    getattr(detail.contract, "lastTradeDateOrContractMonth", "")
                ).strip()
                month_matches = contract_month is None or raw_expiry[:6] == contract_month
                if month_matches and expiry >= today:
                    unexpired_details.append((expiry, detail))
            if not unexpired_details:
                selection = (
                    f" contract_month={contract_month}" if contract_month is not None else ""
                )
                raise ValueError(f"No non-expired future for {symbol} on {exchange}{selection}")
            if contract_month is not None:
                candidates = unexpired_details
            else:
                nearest_expiry = min(item[0] for item in unexpired_details)
                candidates = [item for item in unexpired_details if item[0] == nearest_expiry]
            if len(candidates) != 1:
                selection = (
                    f"contract_month={contract_month}"
                    if contract_month is not None
                    else "front month"
                )
                raise ValueError(
                    f"Ambiguous IBKR future for {symbol} on {exchange} "
                    f"({selection}): {len(candidates)} matches"
                )
            selected = candidates[0][1]
            resolved = selected.contract
            selected_detail = selected

        with self._connection_state_lock:
            if self._connection_generation != generation:
                raise ValueError("IBKR contract response is stale after a connection change")
            self._contract_cache[cache_key] = resolved
            if selected_detail is not None:
                self._contract_details_cache[cache_key] = selected_detail
            return resolved

    def _contract_details(
        self,
        symbol: str,
        *,
        security_type: str,
        exchange: str | None,
        currency: str,
        continuous_alias: bool = False,
        contract_month: str | None = None,
        expected_generation: int | None = None,
    ):
        cache_key = (symbol, security_type, exchange, currency, contract_month)
        with self._connection_state_lock:
            generation = self._connection_generation
            if expected_generation is not None and generation != expected_generation:
                raise ValueError("IBKR connection changed before resolving contract details")

        contract = self._resolve_contract(
            symbol,
            security_type=security_type,
            exchange=exchange,
            currency=currency,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
            expected_generation=generation,
        )
        with self._connection_state_lock:
            if self._connection_generation != generation:
                raise ValueError("IBKR connection changed while resolving contract details")
            cached = self._contract_details_cache.get(cache_key)
            if cached is not None:
                return cached
        details = list(self._ib.reqContractDetails(contract))
        if not details:
            raise ValueError(f"IBKR contract details unavailable for {symbol}")
        contract_id = getattr(contract, "conId", None)
        selected = next(
            (
                item
                for item in details
                if contract_id is not None and getattr(item.contract, "conId", None) == contract_id
            ),
            details[0],
        )
        with self._connection_state_lock:
            if self._connection_generation != generation:
                raise ValueError(
                    "IBKR contract-details response is stale after a connection change"
                )
            self._contract_details_cache[cache_key] = selected
            return selected

    @staticmethod
    def _positive_float(value: object) -> float | None:
        number = IBKRAdapter._optional_float(value)
        return number if number is not None and number > 0 else None


def _parse_dt(dt: datetime | str) -> datetime:
    """Parse a datetime or 'YYYY-MM-DD' string to a UTC-aware datetime."""
    parsed = pd.Timestamp(dt)
    if parsed.tzinfo is None:
        parsed = parsed.tz_localize("UTC")
    return parsed.tz_convert("UTC").to_pydatetime()
