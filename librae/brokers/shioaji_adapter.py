"""ShioajiAdapter — Sinopac Shioaji adapter for Taiwan futures.

Wraps Shioaji SDK using the same flat, duck-typed adapter style as
CryptoAdapter.

Authentication is **required** for all operations (including market data).
Order placement additionally requires CA certificate activation.

Order submission is futures-only: stock (STK) contracts resolve and fetch
market data, but ``prepare_order`` rejects them before submission — there
is no stock settlement account on a certified Sinopac test account.

Credentials can be passed explicitly or loaded from environment variables
using the ``SHIOAJI_`` prefix convention::

    SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY, SHIOAJI_PERSON_ID, SHIOAJI_CA_PATH

Install: ``pip install shioaji`` or ``pip install -e '.[tw-live]'``
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from numbers import Real
from typing import Concatenate

import pandas as pd

from librae.config.env import CredentialConfig, Secret
from librae.config.symbols import (
    AssetClass,
    AvailableSymbol,
    InstrumentKind,
    canonicalize_price_to_increment,
)
from librae.core.trading_calendar import (
    TAIFEX_INDEX_CALENDAR,
    TAIFEX_LATE_OPEN_CALENDAR,
    next_segment_open_after,
    period_close,
    period_start,
    resample_session_ohlcv,
    segment_closes,
    session_lookback_days,
)
from librae.core.utils import (
    floor_to_step,
    interval_to_timedelta,
    validate_contract_month,
    validate_futures_selector,
)
from librae.live.execution_identity import ExecutionIdentity, account_fingerprint
from librae.live.executor import (
    BrokerUnavailableError,
    OrderBelowVenueMinimumError,
    PositionRequest,
)

from .base import (
    AdapterInfo,
    drop_incomplete_ohlcv,
    find_position,
    settled_available_at,
    validate_order_signal,
    validate_time_in_force,
    with_settled_available_at,
)
from .shioaji_stream import ShioajiBarStream
from .shioaji_time import shioaji_kbar_starts

logger = logging.getLogger(__name__)

_TAIPEI = "Asia/Taipei"

# TAIFEX order conditions are ROD, IOC and FOK: there is no multi-day resting
# order, so "gtc" has no equivalent, and a market order cannot be ROD
# (rejected as op_code 9938).
SUPPORTED_TIME_IN_FORCE: dict[str, frozenset[str]] = {
    "limit": frozenset({"day", "ioc", "fok"}),
    "market": frozenset({"ioc", "fok"}),
}


def _require_shioaji():
    """Import and return shioaji, raising a friendly error if missing."""
    try:
        import shioaji

        return shioaji
    except ImportError as e:
        raise ImportError(
            "ShioajiAdapter requires the optional 'tw-live' dependencies. "
            "From a repository clone run: uv sync --extra tw-live. "
            "For a direct install, include Librae's 'tw-live' extra."
        ) from e


# The SDK's own types for a transport failure, a request timeout and server
# maintenance (HTTP 503), per its type stub (shioaji 1.7.0, ``_core.pyi``).
# Which one a network blip raises has not been observed live.
_UNANSWERED_ERRORS = ("ShioajiConnectionError", "ShioajiTimeoutError", "SystemMaintenance")


def _unanswered_reads_unavailable[**P, R](
    read: Callable[Concatenate[ShioajiAdapter, P], R],
) -> Callable[Concatenate[ShioajiAdapter, P], R]:
    """Classify a reconciliation or order status read Shioaji left unanswered.

    Every other SDK error, authentication and token errors included, keeps
    its type and fails closed.
    """

    @functools.wraps(read)
    def wrapper(self: ShioajiAdapter, /, *args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return read(self, *args, **kwargs)
        except Exception as exc:
            # An SDK error implies the SDK is imported; without it nothing classifies.
            sdk = sys.modules.get("shioaji")
            unanswered = tuple(getattr(sdk, name) for name in _UNANSWERED_ERRORS) if sdk else ()
            if not isinstance(exc, unanswered):
                raise
            raise BrokerUnavailableError(str(exc)) from exc

    return wrapper


@dataclass
class ShioajiCredentials(CredentialConfig):
    """Credentials for Sinopac Shioaji API."""

    api_key: Secret = field(default_factory=Secret)
    secret_key: Secret = field(default_factory=Secret)
    person_id: Secret = field(default_factory=Secret)
    ca_path: str = ""
    ca_password: Secret = field(default_factory=Secret)
    sandbox: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.sandbox, str):
            self.sandbox = self.sandbox.lower() == "true"
        # Both or neither: half a pair is a typo'd env var name, and the
        # adapter would otherwise treat it as an intentional read-only run.
        # ca_path/ca_password stay optional — no CA is a supported data-only mode.
        if bool(self.api_key) != bool(self.secret_key):
            raise ValueError(
                "api_key and secret_key must be set together or both left empty "
                "(env: SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY)"
            )


class ShioajiAdapter:
    """Taiwan futures adapter backed by Shioaji SDK (order submission is
    futures-only — see module docstring).

    Parameters
    ----------
    credentials : ShioajiCredentials | None
        If None, loads from env vars with ``SHIOAJI_`` prefix (including
        ``SHIOAJI_SANDBOX``).
    simulation : bool
        If True, use Shioaji simulation mode (paper trading). Mirrors
        CryptoAdapter's ``sandbox`` param — deliberately orthogonal to
        RunConfig.mode (sim/live): mode decides whether LiveExecutor
        submits real orders at all, this decides which Shioaji venue an
        adapter instance talks to. ``credentials.sandbox`` takes
        precedence when both are supplied, same as CryptoAdapter.
    trading_enabled : bool
        If False (default), the CA certificate is never touched and
        ``place_order``/``get_position`` raise ``NotImplementedError`` —
        market data needs no certificate, so a stale ``ca_path`` cannot
        break a data-only caller. If True, the certificate is activated
        during ``__init__`` and an unusable one fails the run at startup
        rather than at its first order. Mirrors IBKRAdapter's flag of the
        same name.

    Unlike CryptoAdapter, Shioaji **requires** login for all operations
    including market data. The adapter logs in during ``__init__``.
    Call ``close()`` or use as context manager to log out.
    """

    market_data_route = "shioaji"

    def market_data_available_at(
        self,
        ts: pd.Timestamp,
        timeframe: str,
        *,
        calendar_id: str,
        session_mode: str,
    ) -> pd.Timestamp:
        """When the bar at ``ts`` is safe to observe: the ``available_at`` a
        fetch stamps on it (``settled_available_at``)."""
        return settled_available_at(ts, timeframe, calendar_id)

    def __init__(
        self,
        credentials: ShioajiCredentials | None = None,
        simulation: bool = False,
        *,
        trading_enabled: bool = False,
    ) -> None:
        sj = _require_shioaji()
        creds = credentials or ShioajiCredentials.from_env("SHIOAJI")
        simulation = creds.sandbox or simulation

        if not creds.api_key or not creds.secret_key:
            raise ValueError(
                "Shioaji requires API credentials for all operations. "
                "Set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY env vars, "
                "or pass ShioajiCredentials explicitly."
            )

        self._api = sj.Shioaji(simulation=simulation)
        # login() takes no person_id (removed upstream — it returns the
        # accounts tied to api_key instead); person_id is still needed by
        # activate_ca() below to pick which account's CA to activate.
        self._api.login(api_key=creds.api_key.reveal(), secret_key=creds.secret_key.reveal())
        self._execution_environment = "paper" if simulation else "production"
        self._bar_stream = ShioajiBarStream(self._api, self._kbars_minutes)
        logger.info("Shioaji login successful (simulation=%s)", simulation)

        self._read_only = True
        if trading_enabled:
            # login() has already succeeded, so any failure here must log out
            # before it leaves the constructor or the session outlives its owner.
            try:
                if not creds.ca_path:
                    raise ValueError(
                        "Shioaji order placement requires a CA certificate: "
                        "set SHIOAJI_CA_PATH, or drop trading_enabled for market data."
                    )
                activated = self._api.activate_ca(
                    ca_path=creds.ca_path,
                    ca_passwd=creds.ca_password.reveal(),
                    person_id=creds.person_id.reveal(),
                )
                # Documented success is True; `is False` rather than a falsy
                # test so a build that returns None on success still passes.
                if activated is False:
                    raise ValueError(f"Shioaji rejected the CA certificate at {creds.ca_path}")
            except Exception:
                self.close()
                raise
            self._read_only = False
            logger.info("Shioaji CA activated — trading enabled")

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="shioaji",
            venue="SINOPAC",
            market_type="tw_futures",
        )

    def execution_identity(self) -> ExecutionIdentity:
        # login() returns whichever accounts the api_key owns — futures/options,
        # stock, or both — and this adapter trades all of them (place_order and
        # get_position both branch on security_type). Fingerprint every account
        # the login actually exposed instead of assuming a futures account:
        # a stock-only login has futopt_account=None and would otherwise be
        # unable to report an identity at all.
        return ExecutionIdentity(
            broker="shioaji",
            environment=self._execution_environment,
            endpoint="sinopac",
            account_fingerprint=account_fingerprint(
                "shioaji",
                *(
                    str(getattr(account, "account_id", "") or "")
                    for account in (
                        getattr(self._api, "futopt_account", None),
                        getattr(self._api, "stock_account", None),
                    )
                ),
            ),
        )

    def available_symbols(
        self,
        *,
        query: str,
        kind: InstrumentKind | None = None,
        asset_class: AssetClass | None = None,
    ) -> tuple[AvailableSymbol, ...]:
        """List every Shioaji futures contract under one product root."""
        root = query.strip().upper()
        if not root:
            raise ValueError("Shioaji available_symbols requires a futures product root")
        if kind not in (None, "future"):
            return ()
        raw_contracts = self._api.contracts.futures(root)
        contracts = list(raw_contracts or [])
        alias_ranks: dict[str, int] = {}
        for contract in contracts:
            code = str(getattr(contract, "code", "") or "")
            target_code = str(getattr(contract, "target_code", "") or "")
            if code.endswith("R1") and target_code:
                alias_ranks[target_code] = 0
            elif code.endswith("R2") and target_code:
                alias_ranks[target_code] = 1

        results: list[AvailableSymbol] = []
        index_roots = {"TXF", "MXF", "TMF"}
        for contract in contracts:
            code = str(getattr(contract, "code", "") or "")
            if not code:
                raise ValueError(f"Shioaji returned a {root} contract without code")
            target_code = str(getattr(contract, "target_code", "") or "")
            is_alias = bool(target_code) or code.endswith(("R1", "R2"))
            delivery_month = validate_contract_month(
                str(getattr(contract, "delivery_month", "") or "") or None
            )
            if is_alias:
                contract_rank = 0 if code.endswith("R1") else 1
                contract_month = None
            else:
                contract_rank = alias_ranks.get(code)
                contract_month = delivery_month
            resolved_asset_class: AssetClass = "index" if root in index_roots else "equity"
            if asset_class is not None and asset_class != resolved_asset_class:
                continue
            raw_multiplier = getattr(contract, "unit", None)
            multiplier = float(raw_multiplier) if raw_multiplier is not None else None
            results.append(
                AvailableSymbol(
                    broker="shioaji",
                    canonical_symbol=code,
                    venue_symbol=code,
                    native_symbol=code,
                    name=str(getattr(contract, "name", "") or code),
                    kind="future",
                    asset_class=resolved_asset_class,
                    currency=str(getattr(contract, "currency", "") or "TWD"),
                    instrument_type="contract_monthly",
                    security_type="FUT",
                    exchange="TAIFEX",
                    contract_month=contract_month,
                    delivery_month=delivery_month,
                    contract_rank=contract_rank,
                    continuous_alias=is_alias,
                    multiplier=multiplier,
                )
            )
        return tuple(
            sorted(
                results,
                key=lambda item: (
                    item.delivery_month or "",
                    item.continuous_alias,
                    item.native_symbol,
                ),
            )
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
        drop_incomplete: bool = False,
        calendar_id: str | None = None,
        continuous_alias: bool = False,
        contract_month: str | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV via Shioaji kbars API.

        Shioaji kbars always returns 1-min bars. When *timeframe* is coarser,
        the result is resampled on the instrument's calendar boundaries.
        Futures default to ``XTAIFEX`` and stocks to ``XTAI``; callers must
        override ``calendar_id`` for products with a different session.

        Shioaji's raw kbar ``ts`` is a Taipei wall clock posing as UTC and
        marks the minute's end; ``shioaji_time.shioaji_kbar_starts`` turns
        it into the UTC minute start before any resampling.

        Args:
            symbol: Contract code (e.g. ``"TXFR1"``, ``"2330"``).
            timeframe: Target candle interval, ccxt or canonical format
                (e.g. ``"1m"``, ``"5m"``, ``"30m"``, ``"1h"``, ``"1d"``) —
                same convention as CryptoAdapter.
            start/end: Taipei dates, as ``"YYYY-MM-DD"`` or a datetime (an
                aware one counts by its Taipei date); ``end`` defaults to
                today and needs ``start``. A daily window the trading
                calendar cannot place, at or past either end of its range,
                raises ``ValueError`` before any query.
                Returns only whole bars: intraday bars starting on these
                dates and daily bars of these trading days, with the minutes
                fetched as far either side as those bars reach (a TAIFEX
                daily bar opens the night before its trading day). If both
                are omitted, fetches the most recent *limit* bars, daily
                ones through such a window of their trading days, from the
                future's tick subscription (see ``shioaji_stream``), since
                Shioaji rules out polling kbars() during the session. That
                path is verified for futures only: a dateless fetch of any
                other contract raises ``ValueError``. A range longer than Shioaji's 30-day
                kbars() query limit is fetched in multiple chunked calls and
                concatenated transparently.
            limit: Max bars (used only when start/end are omitted).
            drop_incomplete: Drop the newest bar unless the source has
                shown it closed (see ``drop_incomplete_ohlcv``), and any bar
                holding a streamed minute that may lack trades.
            calendar_id: Trading-calendar identifier used for resampling.
            continuous_alias: For futures only, require a native R1/R2 route.
            contract_month: For exact futures only, expected ``YYYYMM`` month.

        Returns columns: ``[ts, open, high, low, close, volume, available_at]``
        where ``ts`` is the true UTC-aware bar-start datetime and
        ``available_at`` comes from ``market_data_available_at``.
        """
        contract = self._resolve_contract(symbol)
        self._validate_contract_selection(
            contract,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
        )
        security_type = getattr(contract, "security_type", None)
        dateless = not start and not end
        if dateless and security_type != "FUT":
            # The tick path is verified on TAIFEX futures only; how stock,
            # index and option ticks make kbars() minutes is unverified.
            # kbars() is capped at 270 queries a session
            # (https://sinotrade.github.io/zh/tutor/limit/), so it cannot
            # stand in for it.
            raise ValueError(
                f"Shioaji {symbol!r} ({security_type}) cannot be polled without a date "
                "window: the tick path is verified for futures only, and polling "
                "kbars() would exhaust its query quota; pass start (and end)"
            )
        resolved_calendar = calendar_id or (
            TAIFEX_INDEX_CALENDAR if security_type == "FUT" else "XTAI"
        )

        empty = pd.DataFrame(
            columns=["ts", "open", "high", "low", "close", "volume", "available_at"]
        )
        if dateless and interval_to_timedelta(timeframe) == timedelta(days=1):
            start = _dateless_first_day(limit, resolved_calendar)
        today = pd.Timestamp.now(tz=_TAIPEI).date()
        window = None
        complete_through = None
        if start:
            last_day = _to_date(end) if end else today
            window = _date_window(_to_date(start), last_day, timeframe, resolved_calendar)
            bars_from, bars_before, first_query_day, last_query_day = window
            # No minute stamped after today exists to fetch.
            last_query_day = min(last_query_day, today)
            if bars_from >= bars_before or first_query_day > last_query_day:
                return empty
        elif end:
            raise ValueError("a date window needs start; end alone has no first date")
        else:
            # The stream's seed window: yesterday and today by stamp date.
            first_query_day, last_query_day = today - timedelta(days=1), today
        if dateless:
            df, complete_through = self._bar_stream.minutes(
                contract,
                first_query_day,
                last_query_day,
                resolved_calendar,
                complete_only=drop_incomplete,
            )
        else:
            df = self._kbars_minutes(contract, first_query_day, last_query_day, resolved_calendar)
        if df.empty:
            return empty

        # Resample if requested timeframe is coarser than 1-min
        if timeframe != "1m":
            target_seconds = int(interval_to_timedelta(timeframe).total_seconds())
            # kbars() answers by whole stamp dates (see _date_window), so take
            # a poll's window to open at the Taipei midnight of the first raw
            # stamp (the first minute's end), not at the first minute: a thin
            # contract may not trade at once.
            window_open = (df["ts"].min() + pd.Timedelta(minutes=1)).tz_convert(_TAIPEI).normalize()
            df = resample_session_ohlcv(
                df.set_index("ts"),
                target_seconds,
                resolved_calendar,
            ).reset_index()
            # An intraday bucket that opened before the poll's first date,
            # mid night session, lacks its head: drop it rather than return it
            # as a whole bar.
            if window is None:
                df = df.loc[df["ts"] >= window_open].reset_index(drop=True)
        if window is not None:
            df = df.loc[(df["ts"] >= bars_from) & (df["ts"] < bars_before)].reset_index(drop=True)

        if drop_incomplete:
            if complete_through is not None:
                # The stream left minutes from here to kbars(): a bar reaching
                # past it may lack some even after it closed.
                settled = pd.Timestamp(complete_through, tz=UTC)
                while not df.empty and (
                    period_close(df["ts"].iloc[-1], timeframe, resolved_calendar) > settled
                ):
                    df = df.iloc[:-1]
            df = drop_incomplete_ohlcv(
                df,
                timeframe,
                calendar_id=resolved_calendar,
            )
        if dateless and len(df) > limit:
            df = df.tail(limit).reset_index(drop=True)

        df = with_settled_available_at(
            df[["ts", "open", "high", "low", "close", "volume"]], timeframe, resolved_calendar
        )
        return df.reset_index(drop=True)

    def _kbars_minutes(
        self, contract: object, first_day: date, last_day: date, calendar_id: str
    ) -> pd.DataFrame:
        """kbars() minutes stamped ``first_day`` to ``last_day``, start-stamped
        in UTC, in as few queries as Shioaji's 30-day limit allows."""
        return _kbar_frame(
            (
                self._api.kbars(
                    contract=contract,
                    start=chunk_start.isoformat(),
                    end=chunk_end.isoformat(),
                )
                for chunk_start, chunk_end in _kbar_date_chunks(
                    first_day, last_day, _MAX_KBAR_QUERY_DAYS
                )
            ),
            calendar_id,
        )

    # ------------------------------------------------------------------
    # Order management (requires CA)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "ShioajiAdapter is read-only: built for market data, so no CA "
                "certificate was activated. Pass trading_enabled=True to place orders."
            )

    def prepare_order(self, signal: dict) -> dict:
        """Normalize quantity and validate known fixed-price constraints."""
        validate_order_signal(signal)
        contract = self._resolve_contract(signal["symbol"])
        self._validate_contract_selection(
            contract,
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        if getattr(contract, "security_type", None) == "STK":
            raise ValueError(
                f"{signal['symbol']} is a Taiwan stock; ShioajiAdapter supports "
                "futures order submission only. Stock market data remains "
                "available via fetch_ohlcv."
            )
        prepared = dict(signal)
        quantity = floor_to_step(float(signal["quantity"]), 1.0)
        if quantity < 1:
            raise OrderBelowVenueMinimumError(f"{signal['symbol']} quantity rounds below one lot")
        prepared["quantity"] = quantity

        if signal.get("order_type") == "limit":
            price = float(signal["price"])
            raw_increment = signal.get("price_increment")
            if raw_increment is not None:
                price = canonicalize_price_to_increment(
                    price,
                    raw_increment,
                    context=f"{signal['symbol']} limit price",
                )
            raw_lower = getattr(contract, "limit_down", None)
            raw_upper = getattr(contract, "limit_up", None)
            if raw_lower is None or raw_upper is None:
                raise ValueError(f"{signal['symbol']} contract is missing price-limit boundaries")
            lower = float(raw_lower)
            upper = float(raw_upper)
            if not isfinite(lower) or not isfinite(upper) or lower <= 0 or upper <= lower:
                raise ValueError(f"{signal['symbol']} contract has invalid price-limit boundaries")
            if price < lower:
                raise ValueError(f"{signal['symbol']} price is below limit_down {lower}")
            if price > upper:
                raise ValueError(f"{signal['symbol']} price exceeds limit_up {upper}")
            # A missing fixed increment is intentional for price-band markets.
            # Preserve the requested price and let Shioaji/the venue perform
            # its authoritative variable-tick validation.
            prepared["price"] = price
        return prepared

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side`` (``"buy"``/``"sell"``),
        ``quantity``, ``order_type`` (``"market"``/``"limit"``),
        ``time_in_force`` (``"day"``/``"ioc"``/``"fok"`` — no ``"gtc"``),
        and optionally ``price`` for limit orders.

        ``custom_field`` is assumed to hold six characters (unverified against
        the venue: the SDK declares it ``Optional[str]`` with no length bound),
        so a deterministic base32 digest of ``client_order_id`` is used for
        restart lookup. Duplicate digest matches fail closed instead of
        guessing ownership.
        """
        self._require_auth()
        validate_order_signal(signal)

        sj = _require_shioaji()
        contract = self._resolve_contract(signal["symbol"])
        self._validate_contract_selection(
            contract,
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        is_futures = getattr(contract, "security_type", None) == "FUT"

        # shioaji >=1.4 moved these enums from shioaji.order.* to top-level
        # shioaji.* (member names unchanged) — sj.order.Action etc. raises
        # AttributeError on current versions.
        action = sj.Action.Buy if signal["side"] == "buy" else sj.Action.Sell
        is_limit = signal["order_type"] == "limit"
        if is_futures:
            price_type = sj.FuturesPriceType.LMT if is_limit else sj.FuturesPriceType.MKT
        else:
            price_type = sj.StockPriceType.LMT if is_limit else sj.StockPriceType.MKT

        # Market orders (MKT) are rejected by TAIFEX/TWSE with ROD (rest-of-day)
        # time-in-force -- confirmed live 2026-07-20, op_code 9938: "市價單不允許
        # 當日有效委託(ROD)". A market order that stays resting all day is a
        # contradiction in terms; it must be IOC or FOK. Both rules live in
        # SUPPORTED_TIME_IN_FORCE, which the run's preflight reads too.
        time_in_force = signal["time_in_force"]
        validate_time_in_force(
            "shioaji",
            SUPPORTED_TIME_IN_FORCE,
            "limit" if is_limit else "market",
            time_in_force,
        )
        order_type = {
            "day": sj.OrderType.ROD,
            "ioc": sj.OrderType.IOC,
            "fok": sj.OrderType.FOK,
        }[time_in_force]

        # shioaji >=1.5 deprecated the generic Order() in favour of
        # StockOrder()/FuturesOrder() (place_order's type hint is now
        # Union[StockOrder, FuturesOrder]) — confirmed via DeprecationWarning
        # running against a real sandbox session 2026-07-26.
        order_cls = sj.FuturesOrder if is_futures else sj.StockOrder
        quantity = float(signal["quantity"])
        if not quantity.is_integer() or quantity < 1:
            raise ValueError("Shioaji quantity must be a positive whole lot; call prepare_order")
        order_kwargs = dict(
            price=signal.get("price", 0),
            quantity=int(quantity),
            action=action,
            price_type=price_type,
            order_type=order_type,
        )
        if signal.get("client_order_id"):
            order_kwargs["custom_field"] = self.broker_client_order_id(signal["client_order_id"])
        order = order_cls(**order_kwargs)
        trade = self._api.place_order(contract, order)
        return self._trade_to_order(trade, symbol=signal["symbol"])

    @_unanswered_reads_unavailable
    def find_order(self, client_order_id: str, symbol: str) -> dict | None:
        """Find an order by its deterministic six-character custom field."""
        self._require_auth()
        tag = self.broker_client_order_id(client_order_id)
        matches = [
            trade
            for trade in self._trades(symbol)
            if str(getattr(trade.order, "custom_field", "") or "") == tag
        ]
        if len(matches) > 1:
            raise ValueError(f"duplicate Shioaji custom_field digest: {tag}")
        return self._trade_to_order(matches[0], symbol=symbol) if matches else None

    @_unanswered_reads_unavailable
    def get_order(self, order_id: str, symbol: str) -> dict:
        """Refresh and return one cumulative Shioaji Trade state."""
        self._require_auth()
        for trade in self._trades(symbol):
            status_id = str(getattr(trade.status, "id", "") or "")
            order_obj_id = str(getattr(trade.order, "id", "") or "")
            if order_id in (status_id, order_obj_id):
                return self._trade_to_order(trade, symbol=symbol)
        raise LookupError(f"Shioaji order not found: {order_id}")

    @_unanswered_reads_unavailable
    def list_open_orders(self, symbol: str) -> list[dict]:
        """Return non-final orders after refreshing the account state."""
        open_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
        return [
            self._trade_to_order(trade, symbol=symbol)
            for trade in self._trades(symbol)
            if str(
                getattr(
                    getattr(trade.status, "status", ""),
                    "value",
                    getattr(trade.status, "status", ""),
                )
            )
            in open_statuses
        ]

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel a Shioaji Trade object and return its refreshed state.

        Its reads stay unclassified: once the cancel went out, a refresh that
        fails after it must not say the call had no effect.
        """
        self._require_auth()
        for trade in self._trades(symbol):
            status_id = str(getattr(trade.status, "id", "") or "")
            order_obj_id = str(getattr(trade.order, "id", "") or "")
            if order_id in (status_id, order_obj_id):
                self._api.cancel_order(trade)
                self._api.update_status(trade=trade)
                return self._trade_to_order(trade, symbol=symbol)
        raise LookupError(f"Shioaji order not found: {order_id}")

    def _trades(self, symbol: str) -> list:
        contract = self._resolve_contract(symbol)
        contract_codes = self._contract_codes(contract)
        self._api.update_status()
        return [
            trade
            for trade in self._api.list_trades()
            if str(getattr(trade.contract, "code", "")) in contract_codes
        ]

    def broker_client_order_id(self, client_order_id: str) -> str:
        """Map Librae's durable id to the six-character custom field this adapter assumes."""
        digest = hashlib.sha256(client_order_id.encode()).digest()
        return base64.b32encode(digest).decode("ascii")[:6]

    @staticmethod
    def _trade_to_order(trade, *, symbol: str | None = None) -> dict:
        """Translate a Shioaji Trade into one cumulative execution report."""
        status = trade.status
        order = trade.order
        deals = list(getattr(status, "deals", None) or [])
        requested = getattr(status, "order_quantity", None)
        if not isinstance(requested, Real) or requested <= 0:
            # A Trade returned straight from place_order may not carry the
            # venue-side order_quantity yet; the order we sent is the
            # requested quantity and must match the tracked request exactly.
            requested = getattr(order, "quantity", 0)
        filled = getattr(status, "deal_quantity", 0)
        filled = float(filled) if isinstance(filled, Real) else 0.0
        result = {
            "id": str(getattr(status, "id", "") or getattr(order, "id", "") or ""),
            "status": str(
                getattr(
                    getattr(status, "status", "unknown"),
                    "value",
                    getattr(status, "status", "unknown"),
                )
            ),
            "amount": float(requested),
            "filled": filled,
        }
        client_order_id = getattr(order, "custom_field", None)
        if isinstance(client_order_id, str) and client_order_id:
            result["clientOrderId"] = client_order_id
        if symbol is not None:
            # The caller resolves and filters the concrete Shioaji contract for
            # this venue symbol before mapping it back to the shared contract.
            result["symbol"] = symbol
        raw_side = getattr(order, "action", None)
        side = getattr(raw_side, "value", raw_side)
        if isinstance(side, str) and side.lower() in ("buy", "sell"):
            result["side"] = side.lower()
        if filled and deals:
            result["average"] = (
                sum(float(deal.price) * float(deal.quantity) for deal in deals) / filled
            )
            result["executed_at"] = max(
                getattr(deal, "datetime", None) or datetime.fromtimestamp(float(deal.ts), tz=UTC)
                for deal in deals
            )
            commissions = [
                float(deal.commission)
                for deal in deals
                if isinstance(getattr(deal, "commission", None), Real)
            ]
            if len(commissions) == len(deals):
                result["commission"] = sum(commissions)
        return result

    @_unanswered_reads_unavailable
    def get_position(self, request: PositionRequest) -> dict:
        """Return the current position for one configured instrument."""
        self._require_auth()
        sj = _require_shioaji()
        symbol = request.venue_symbol
        contract = self._resolve_contract(symbol)
        self._validate_contract_selection(
            contract,
            continuous_alias=request.continuous_alias,
            contract_month=request.contract_month,
        )
        contract_codes = self._contract_codes(contract)
        account = (
            self._api.futopt_account
            if getattr(contract, "security_type", None) == "FUT"
            else self._api.stock_account
        )
        positions = self._api.list_positions(account=account)

        def signed_quantity(position: object) -> float:
            quantity = float(position.quantity)
            if not isfinite(quantity) or quantity < 0:
                raise ValueError(f"invalid Shioaji position quantity for {symbol}: {quantity!r}")
            if quantity == 0:
                return 0.0
            if position.direction == sj.Action.Buy:
                return quantity
            if position.direction == sj.Action.Sell:
                return -quantity
            raise ValueError(
                f"unknown Shioaji position direction for {symbol}: {position.direction!r}"
            )

        return find_position(
            positions,
            request.symbol,
            matches=lambda p: str(p.code) in contract_codes,
            size=signed_quantity,
            avg_price=lambda p: p.price,
            pnl=lambda p: getattr(p, "pnl", 0),
        )

    @_unanswered_reads_unavailable
    def get_balance(self, currency: str) -> dict[str, float]:
        """Return futures account margin balance (TWD only — Shioaji futures
        accounts don't hold other currencies).

        Verified against a live Shioaji sandbox session on 2026-07-26:
        ``margin()`` returns ``Margin(equity=..., available_margin=...,
        risk_indicator=...)`` — ``equity``, not ``equity_amount`` (reading
        that name would silently set ``total``/``used`` to 0 regardless of
        actual account equity).
        """
        self._require_auth()
        if currency != "TWD":
            raise ValueError(f"Shioaji futures balance supports TWD only, got {currency!r}")
        margin = self._api.margin()
        raw_total = getattr(margin, "equity", None)
        raw_free = getattr(margin, "available_margin", None)
        if raw_total is None or raw_free is None:
            raise ValueError("Shioaji margin is missing equity or available_margin")
        total = float(raw_total)
        free = float(raw_free)
        if not isfinite(total) or not isfinite(free):
            raise ValueError("Shioaji margin contains non-finite balance values")
        return {"free": free, "used": total - free, "total": total}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Logout from Shioaji API."""
        try:
            self._api.logout()
            logger.info("Shioaji logout successful")
        except Exception as e:
            logger.warning("Shioaji logout failed: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_contract(self, symbol: str):
        """Resolve a symbol string to a Shioaji contract object."""
        # api.Contracts (capitalised, split .Futures/.Stocks lookup) is
        # deprecated in favour of api.contracts (lowercase, "contracts v2")
        # — confirmed via DeprecationWarning against a real sandbox session
        # 2026-07-26. api.contracts.futures/.stocks turned out to be
        # *methods*, not sub-containers with a .get() like the legacy API
        # (confirmed via AttributeError against the same session) — the v2
        # container instead exposes a single unified .get(symbol) covering
        # futures/stocks/options/index, so there's no fallback branch left.
        contract = self._api.contracts.get(symbol)
        if contract is None:
            raise ValueError(f"Unknown symbol: {symbol}")
        return contract

    def _validate_contract_selection(
        self,
        contract: object,
        *,
        continuous_alias: bool,
        contract_month: str | None,
    ) -> None:
        """Verify the common contract identity against Shioaji metadata."""
        if not isinstance(continuous_alias, bool):
            raise TypeError("continuous_alias must be a bool")
        contract_month = validate_contract_month(contract_month)
        is_futures = getattr(contract, "security_type", None) == "FUT"
        if not is_futures:
            if continuous_alias or contract_month is not None:
                raise ValueError(
                    "continuous_alias and contract_month are valid only for Shioaji futures"
                )
            return
        validate_futures_selector(
            continuous_alias,
            contract_month,
            context="Shioaji future",
            alias_meaning="the venue's native R1/R2 alias",
        )

        code = str(getattr(contract, "code", "") or "")
        target_code = str(getattr(contract, "target_code", "") or "")
        is_native_alias = bool(target_code) or code.endswith(("R1", "R2"))
        if continuous_alias:
            if not is_native_alias:
                raise ValueError(f"Shioaji contract {code!r} is not a continuous R1/R2 alias")
            return
        if is_native_alias:
            raise ValueError(
                f"Shioaji exact contract_month={contract_month} cannot use "
                f"continuous alias {code!r}"
            )

        info = self._api.contracts.info(contract)
        resolved_month = str(
            getattr(info, "delivery_month", None) or getattr(contract, "delivery_month", "") or ""
        )
        if not resolved_month:
            raise ValueError(f"Shioaji contract {code!r} has no delivery_month metadata")
        if resolved_month != contract_month:
            raise ValueError(
                f"Shioaji contract month mismatch for {code!r}: "
                f"configured={contract_month}, broker={resolved_month}"
            )

    @staticmethod
    def _contract_codes(contract: object) -> set[str]:
        """Return native codes that may appear in Shioaji order/position state."""
        codes = {
            str(value)
            for value in (
                getattr(contract, "code", None),
                getattr(contract, "target_code", None),
            )
            if value
        }
        if not codes:
            raise ValueError("Shioaji contract has no stable code")
        return codes


def _to_date(dt: datetime | str) -> date:
    """The Taipei date of ``dt``; a naive datetime is read as Taipei time."""
    if isinstance(dt, str):
        # A string with a time would have to be read in some time zone; pass
        # a datetime for that, so there is one rule for it.
        try:
            return date.fromisoformat(dt)
        except ValueError:
            raise ValueError(f"expected a 'YYYY-MM-DD' date string, got {dt!r}") from None
    if dt.tzinfo is not None:
        return pd.Timestamp(dt).tz_convert(_TAIPEI).date()
    return dt.date()


def _taipei_midnight(day: date) -> pd.Timestamp:
    return pd.Timestamp(day, tz=_TAIPEI).tz_convert(UTC)


def _date_window(
    first_day: date, last_day: date, timeframe: str, calendar_id: str
) -> tuple[pd.Timestamp, pd.Timestamp, date, date]:
    """Bar starts ``[from, before)`` of the bars dated ``first_day`` to
    ``last_day``, and the first and last kbars dates holding their minutes.

    A daily bar is dated by its trading day and an intraday bar by the Taipei
    date it starts on. So a TAIFEX daily bar starts at the previous trading
    day's night open, and an intraday bar starting late on ``last_day`` runs
    into the next date. kbars() dates a minute by its raw stamp, the minute's
    end, so the minute ending at midnight belongs to the next date. Observed
    on 2026-11-04: TXFR1 kbars for 2026-10-31 to 2026-11-03 ran from stamp
    10-31 00:00 to 11-03 23:59, Saturday's night minutes included, and were
    the very rows a 2026-10-21 to 2026-11-04 query held on those dates.
    """
    if interval_to_timedelta(timeframe) >= timedelta(days=1):

        def first_open(day: date) -> pd.Timestamp:
            """Open of the first trading day on or after ``day``."""
            segment_open = next_segment_open_after(_taipei_midnight(day), calendar_id)
            return period_start(segment_open, "D1", calendar_id)

        bars_from = first_open(first_day)
        bars_before = first_open(last_day + timedelta(days=1))
        # A trading day closes on its own date.
        last_stamp_day = last_day
    else:
        bars_from = _taipei_midnight(first_day)
        bars_before = _taipei_midnight(last_day + timedelta(days=1))
        last_minute = bars_before - pd.Timedelta(minutes=1)
        try:
            bar_start = period_start(last_minute, timeframe, calendar_id)
            last_stamp = period_close(bar_start, timeframe, calendar_id)
        except ValueError:  # no session at the window's last minute
            last_stamp = last_minute
        last_stamp_day = last_stamp.tz_convert(_TAIPEI).date()
    first_stamp_day = (bars_from + pd.Timedelta(minutes=1)).tz_convert(_TAIPEI).date()
    return bars_from, bars_before, first_stamp_day, last_stamp_day


# Shioaji rejects a single kbars() query spanning more than this many days:
# https://sinotrade.github.io/zh/tutor/limit/
_MAX_KBAR_QUERY_DAYS = 30

# A poll spends the intraday kbars() quota, so a dateless poll of daily bars
# looks back at most this far: one query even after _date_window widens it to
# the trading night before (12 days over the 2026 Lunar New Year).
_DATELESS_DAILY_LOOKBACK_DAYS = 14


def _dateless_first_day(limit: int, calendar_id: str) -> str:
    """First date of the window a dateless poll of daily bars asks for.

    A dateless kbars() call answers yesterday and today by stamp date, its
    documented default (https://sinotrade.github.io/zh/tutor/market_data/historical/;
    on 2026-11-05 it started at stamp 11-04 00:00). A TAIFEX daily bar opens
    the evening before its trading day, a Monday's on Friday, so that window
    cuts them; their trading days are asked for by date instead.
    """
    now = pd.Timestamp.now(tz=_TAIPEI)
    # No more sessions than days fit in the cap, so a warmup-sized limit
    # cannot reach past the calendar.
    sessions = min(limit + 1, _DATELESS_DAILY_LOOKBACK_DAYS)
    days = min(session_lookback_days(now, sessions, calendar_id), _DATELESS_DAILY_LOOKBACK_DAYS)
    return (now.date() - timedelta(days=days)).isoformat()


def _kbar_frame(responses: Iterable[Mapping[str, list]], calendar_id: str) -> pd.DataFrame:
    """kbars() answers as one frame of lower-case columns and minute starts,
    each segment's closing trades in its last minute."""
    frames = [frame for response in responses if not (frame := pd.DataFrame({**response})).empty]
    if not frames:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.concat(frames, ignore_index=True)
    # Shioaji names the price columns capitalised.
    df.columns = df.columns.str.lower()
    df["ts"] = shioaji_kbar_starts(df["ts"])
    return _fold_closing_minutes(
        df.sort_values("ts", kind="stable", ignore_index=True), calendar_id
    )


def _fold_closing_minutes(df: pd.DataFrame, calendar_id: str) -> pd.DataFrame:
    """Merge a TAIFEX minute starting at a segment close into the segment's last minute.

    TAIFEX matches trades at a segment's closing instant, and for some
    contracts kbars() puts them in a minute of their own stamped one past
    the close, which starts at the close and so lies outside every segment.
    Observed for TMF 202611 on 2026-11-05: TAIFEX's tick file has its last
    day-session trade at 13:45:00 (41905, 1 lot), which kbars() returns
    stamped 13:46; night trades at 05:00:00 come stamped 05:01 the same way.
    TXF and MTX kbars hold them in the minute stamped at the close, which is
    also where the tick stream counts a closing print. A minute that starts
    at a close holds only those trades, since nothing trades between
    segments; any other minute outside the segments is left for the
    resampler to handle.

    TAIFEX calendars only: stock and index kbars past the close are
    unverified, and index kbars run minutes past it (see
    ``shioaji_kbar_starts``), so a minute there is not known to hold the
    closing trades.
    """
    if calendar_id not in (TAIFEX_INDEX_CALENDAR, TAIFEX_LATE_OPEN_CALENDAR):
        return df
    starts = pd.DatetimeIndex(df["ts"])
    previous = starts - pd.Timedelta(minutes=1)
    at_close = segment_closes(previous, "1m", calendar_id) == starts
    at_close &= segment_closes(starts, "1m", calendar_id).isna()
    if not at_close.any():
        return df
    closing = df.loc[at_close].assign(ts=previous[at_close])
    rest = df.loc[~at_close]
    last = rest["ts"].isin(closing["ts"])
    rules = {column: "sum" for column in df.columns if column != "ts"}
    rules.update(open="first", high="max", low="min", close="last")
    # Only each closing minute and the last minute it joins are merged.
    merged = (
        pd.concat([rest.loc[last], closing])
        .sort_values("ts", kind="stable")
        .groupby("ts", sort=True)
        .agg(rules)
        .reset_index()
    )
    return pd.concat([rest.loc[~last], merged]).sort_values("ts", kind="stable", ignore_index=True)


def _kbar_date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into consecutive windows no wider than max_days."""
    chunks = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=max_days - 1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return chunks
