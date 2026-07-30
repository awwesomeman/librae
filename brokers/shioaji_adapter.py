"""ShioajiAdapter — Sinopac Shioaji adapter for Taiwan futures/stocks.

Wraps Shioaji SDK using the same flat, duck-typed adapter style as
CryptoAdapter.

Authentication is **required** for all operations (including market data).
Order placement additionally requires CA certificate activation.

Credentials can be passed explicitly or loaded from environment variables
using the ``SHIOAJI_`` prefix convention::

    SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY, SHIOAJI_PERSON_ID, SHIOAJI_CA_PATH

Install: ``pip install shioaji`` or ``pip install -e '.[tw-live]'``
"""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from numbers import Real

import pandas as pd
from librae.config.symbols import (
    AssetClass,
    AvailableSymbol,
    InstrumentKind,
)
from librae.core.trading_calendar import (
    TAIFEX_INDEX_CALENDAR,
    resample_session_ohlcv,
)
from librae.core.utils import validate_contract_month
from librae.live.executor import PositionRequest

from .base import (
    AdapterInfo,
    CredentialConfig,
    drop_incomplete_ohlcv,
    find_position,
    floor_to_step,
    passive_price,
    validate_order_signal,
)
from .shioaji_time import shioaji_ts_ns_to_epoch

logger = logging.getLogger(__name__)


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


@dataclass
class ShioajiCredentials(CredentialConfig):
    """Credentials for Sinopac Shioaji API."""

    api_key: str = ""
    secret_key: str = ""
    person_id: str = ""
    ca_path: str = ""
    ca_password: str = ""
    sandbox: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.sandbox, str):
            self.sandbox = self.sandbox.lower() == "true"


class ShioajiAdapter:
    """Taiwan futures/stocks adapter backed by Shioaji SDK.

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

    Unlike CryptoAdapter, Shioaji **requires** login for all operations
    including market data. The adapter logs in during ``__init__``.
    Call ``close()`` or use as context manager to log out.
    """

    def __init__(
        self,
        credentials: ShioajiCredentials | None = None,
        simulation: bool = False,
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
        self._api.login(api_key=creds.api_key, secret_key=creds.secret_key)
        logger.info("Shioaji login successful (simulation=%s)", simulation)

        self._read_only = True
        if creds.ca_path:
            self._api.activate_ca(
                ca_path=creds.ca_path,
                ca_passwd=creds.ca_password,
                person_id=creds.person_id,
            )
            self._read_only = False
            logger.info("Shioaji CA activated — trading enabled")

    def info(self) -> AdapterInfo:
        return AdapterInfo(
            adapter_id="shioaji",
            venue="SINOPAC",
            market_type="tw_futures",
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
            resolved_asset_class: AssetClass = (
                "index" if root in index_roots else "equity"
            )
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

        Shioaji's raw kbar ``ts`` encodes Taipei wall-clock time as if it
        were a UTC epoch; this is corrected to a true UTC epoch before
        returning (``shioaji_time.shioaji_ts_ns_to_epoch``).

        Args:
            symbol: Contract code (e.g. ``"TXFR1"``, ``"2330"``).
            timeframe: Target candle interval, ccxt or canonical format
                (e.g. ``"1m"``, ``"5m"``, ``"30m"``, ``"1h"``, ``"1d"``) —
                same convention as CryptoAdapter.
            start/end: Date range as datetime or ``"YYYY-MM-DD"`` string.
                If omitted, fetches the most recent *limit* bars.
            limit: Max bars (used only when start/end are omitted).
            drop_incomplete: Drop the current still-forming candle.
            calendar_id: Trading-calendar identifier used for resampling.
            continuous_alias: For futures only, require a native R1/R2 route.
            contract_month: For exact futures only, expected ``YYYYMM`` month.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is the true UTC-aware bar-start datetime.
        """
        contract = self._resolve_contract(symbol)
        self._validate_contract_selection(
            contract,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
        )
        resolved_calendar = calendar_id or (
            TAIFEX_INDEX_CALENDAR if getattr(contract, "security_type", None) == "FUT" else "XTAI"
        )

        kbar_kwargs: dict = {}
        if start:
            kbar_kwargs["start"] = _to_date_str(start)
        if end:
            kbar_kwargs["end"] = _to_date_str(end)

        kbars = self._api.kbars(
            contract=contract,
            **kbar_kwargs,
        )

        df = pd.DataFrame({**kbars})
        if df.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        # Normalise column names (Shioaji may return capitalised names)
        df.columns = df.columns.str.lower()
        df["ts"] = pd.to_datetime(df["ts"].map(shioaji_ts_ns_to_epoch), unit="s", utc=True)

        # Resample if requested timeframe is coarser than 1-min
        if timeframe != "1m":
            from librae.core.utils import interval_to_timedelta

            target_seconds = int(interval_to_timedelta(timeframe).total_seconds())
            df = resample_session_ohlcv(
                df.set_index("ts"),
                target_seconds,
                resolved_calendar,
            ).reset_index()

        if drop_incomplete:
            df = drop_incomplete_ohlcv(
                df,
                timeframe,
                calendar_id=resolved_calendar,
            )
        if not start and not end and len(df) > limit:
            df = df.tail(limit).reset_index(drop=True)

        return df[["ts", "open", "high", "low", "close", "volume"]]

    # ------------------------------------------------------------------
    # Order management (requires CA)
    # ------------------------------------------------------------------

    def _require_auth(self) -> None:
        if self._read_only:
            raise NotImplementedError(
                "CA certificate not activated — ShioajiAdapter is in read-only mode. "
                "Provide ca_path to enable trading."
            )

    def prepare_order(self, signal: dict) -> dict:
        """Round quantity/limit price to Shioaji contract rules."""
        validate_order_signal(signal)
        contract = self._resolve_contract(signal["symbol"])
        self._validate_contract_selection(
            contract,
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
        )
        prepared = dict(signal)
        quantity = floor_to_step(float(signal["quantity"]), 1.0)
        if quantity < 1:
            raise ValueError(f"{signal['symbol']} quantity rounds below one lot")
        prepared["quantity"] = quantity

        if signal.get("order_type") == "limit":
            tick_size = float(signal.get("tick_size") or 0.0)
            if tick_size <= 0:
                raise ValueError("Shioaji limit orders require a positive tick_size")
            price = passive_price(float(signal["price"]), tick_size, signal["side"])
            raw_lower = getattr(contract, "limit_down", None)
            raw_upper = getattr(contract, "limit_up", None)
            if raw_lower is None or raw_upper is None:
                raise ValueError(
                    f"{signal['symbol']} contract is missing price-limit boundaries"
                )
            lower = float(raw_lower)
            upper = float(raw_upper)
            if not isfinite(lower) or not isfinite(upper) or lower <= 0 or upper <= lower:
                raise ValueError(
                    f"{signal['symbol']} contract has invalid price-limit boundaries"
                )
            if price < lower:
                raise ValueError(f"{signal['symbol']} price is below limit_down {lower}")
            if price > upper:
                raise ValueError(f"{signal['symbol']} price exceeds limit_up {upper}")
            prepared["price"] = price
        return prepared

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side`` (``"buy"``/``"sell"``),
        ``quantity``, ``order_type`` (``"market"``/``"limit"``),
        and optionally ``price`` for limit orders.

        Shioaji caps ``custom_field`` at six characters, so a deterministic
        base32 digest of ``client_order_id`` is used for restart lookup.
        Duplicate digest matches fail closed instead of guessing ownership.
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
        # contradiction in terms; it must be IOC (fill immediately or cancel).
        # Limit orders keep ROD, which is a legitimate resting order.
        order_type = sj.OrderType.ROD if is_limit else sj.OrderType.IOC

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
            order_kwargs["custom_field"] = self._client_tag(signal["client_order_id"])
        order = order_cls(**order_kwargs)
        trade = self._api.place_order(contract, order)
        return {
            "id": trade.status.id if trade.status else "",
            "status": trade.status.status if trade.status else "unknown",
        }

    def find_order(self, client_order_id: str, symbol: str) -> dict | None:
        """Find an order by its deterministic six-character custom field."""
        self._require_auth()
        tag = self._client_tag(client_order_id)
        matches = [
            trade
            for trade in self._trades(symbol)
            if str(getattr(trade.order, "custom_field", "") or "") == tag
        ]
        if len(matches) > 1:
            raise ValueError(f"duplicate Shioaji custom_field digest: {tag}")
        return self._trade_to_order(matches[0]) if matches else None

    def get_order(self, order_id: str, symbol: str) -> dict:
        """Refresh and return one cumulative Shioaji Trade state."""
        self._require_auth()
        for trade in self._trades(symbol):
            status_id = str(getattr(trade.status, "id", "") or "")
            order_obj_id = str(getattr(trade.order, "id", "") or "")
            if order_id in (status_id, order_obj_id):
                return self._trade_to_order(trade)
        raise LookupError(f"Shioaji order not found: {order_id}")

    def list_open_orders(self, symbol: str) -> list[dict]:
        """Return non-final orders after refreshing the account state."""
        open_statuses = {"PendingSubmit", "PreSubmitted", "Submitted", "PartFilled"}
        return [
            self._trade_to_order(trade)
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
        """Cancel a Shioaji Trade object and return its refreshed state."""
        self._require_auth()
        for trade in self._trades(symbol):
            status_id = str(getattr(trade.status, "id", "") or "")
            order_obj_id = str(getattr(trade.order, "id", "") or "")
            if order_id in (status_id, order_obj_id):
                self._api.cancel_order(trade)
                self._api.update_status(trade=trade)
                return self._trade_to_order(trade)
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

    @staticmethod
    def _client_tag(client_order_id: str) -> str:
        digest = hashlib.sha256(client_order_id.encode()).digest()
        return base64.b32encode(digest).decode("ascii")[:6]

    @staticmethod
    def _trade_to_order(trade) -> dict:
        """Translate a Shioaji Trade into one cumulative execution report."""
        status = trade.status
        order = trade.order
        deals = list(getattr(status, "deals", None) or [])
        requested = getattr(status, "order_quantity", None)
        if not isinstance(requested, Real):
            requested = getattr(order, "quantity", 0)
        filled = getattr(status, "deal_quantity", 0)
        filled = float(filled) if isinstance(filled, Real) else 0.0
        result = {
            "id": str(getattr(status, "id", "") or getattr(order, "id", "") or ""),
            "clientOrderId": str(getattr(order, "custom_field", "") or ""),
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

    def get_balance(self, currency: str) -> dict[str, float]:
        """Return futures account margin balance (TWD only — Shioaji futures
        accounts don't hold other currencies).

        Verified against a live Shioaji sandbox session on 2026-07-26:
        ``margin()`` returns ``Margin(equity=..., available_margin=...,
        risk_indicator=...)`` — ``equity``, not ``equity_amount`` as
        previously assumed (that name never matched, so ``total``/``used``
        silently fell back to 0 regardless of actual account equity).
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
        if continuous_alias == (contract_month is not None):
            raise ValueError(
                "Shioaji future requires exactly one of continuous_alias=True "
                "or contract_month='YYYYMM'"
            )

        code = str(getattr(contract, "code", "") or "")
        target_code = str(getattr(contract, "target_code", "") or "")
        is_native_alias = bool(target_code) or code.endswith(("R1", "R2"))
        if continuous_alias:
            if not is_native_alias:
                raise ValueError(
                    f"Shioaji contract {code!r} is not a continuous R1/R2 alias"
                )
            return
        if is_native_alias:
            raise ValueError(
                f"Shioaji exact contract_month={contract_month} cannot use "
                f"continuous alias {code!r}"
            )

        info = self._api.contracts.info(contract)
        resolved_month = str(
            getattr(info, "delivery_month", None)
            or getattr(contract, "delivery_month", "")
            or ""
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


def _to_date_str(dt: datetime | str) -> str:
    """Convert datetime or string to 'YYYY-MM-DD' format."""
    if isinstance(dt, str):
        return dt[:10]
    return dt.strftime("%Y-%m-%d")
