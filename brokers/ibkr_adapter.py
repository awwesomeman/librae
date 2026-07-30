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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isclose, isfinite

import pandas as pd
from librae.config.symbols import (
    AssetClass,
    AvailableSymbol,
    InstrumentKind,
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


def _utc_today() -> date:
    """Return the UTC date used for futures-expiry decisions."""
    return datetime.now(UTC).date()


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
        self._contract_cache: dict[
            tuple[str, str, str | None, str, str | None], object
        ] = {}
        self._contract_details_cache: dict[
            tuple[str, str, str | None, str, str | None], object
        ] = {}
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
                raw_expiry = str(
                    getattr(resolved, "lastTradeDateOrContractMonth", "")
                ).strip()
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
            expiry: rank
            for rank, expiry in enumerate(sorted({item[0] for item in unexpired}))
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
        use_rth: bool = False,
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
            use_rth: ``False`` (default, unchanged from prior behavior)
                includes extended-hours prints. If a live run built on this
                adapter fetches with a different ``use_rth`` than whatever
                produced its backtest's historical data, the two see a
                different bar shape for the same nominal timeframe (extra
                pre/post-market bars, different daily OHLC) — this adapter
                can't detect that mismatch itself since it doesn't know
                where the backtest data came from; the caller is
                responsible for passing the same value both places.
            drop_incomplete: Drop the current still-forming candle.

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is the UTC-aware bar-start datetime.

        IBKR's own pacing/lookback limits per bar size (e.g. 1-sec bars only
        go back a few days) apply and aren't paginated around here; a
        window too long for the requested bar size raises from ib_async
        directly.
        """
        contract = self._resolve_contract(
            symbol,
            security_type=security_type,
            exchange=exchange,
            currency=currency,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
        )
        bar_size = _to_bar_size(timeframe)

        end_dt = _parse_dt(end) if end else datetime.now(UTC)
        if start:
            start_dt = _parse_dt(start)
            duration = f"{max(1, (end_dt - start_dt).days + 1)} D"
        else:
            duration = _default_duration_str(timeframe, limit)

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime=end_dt,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=use_rth,
            formatDate=2,  # UTC datetimes, not exchange-local strings
        )

        ib_async = _require_ib_async()
        df = ib_async.util.df(bars)
        if df is None or df.empty:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])

        df = df.rename(columns={"date": "ts"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df[["ts", "open", "high", "low", "close", "volume"]]

        if start:
            df = df[(df["ts"] >= start_dt) & (df["ts"] <= end_dt)]
        elif len(df) > limit:
            df = df.tail(limit)
        if drop_incomplete:
            df = drop_incomplete_ohlcv(df, timeframe)
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

    def prepare_order(self, signal: dict) -> dict:
        """Apply IBKR ContractDetails size and tick constraints."""
        validate_order_signal(signal)
        details = self._contract_details(
            signal["symbol"],
            security_type=signal["security_type"],
            exchange=signal.get("exchange"),
            currency=signal["currency"],
            continuous_alias=signal.get("continuous_alias", False),
            contract_month=signal.get("contract_month"),
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
            tick_size = self._positive_float(getattr(details, "minTick", None))
            if tick_size is None:
                raise ValueError(f"{signal['symbol']} has no positive IBKR minTick")
            prepared["price"] = passive_price(
                float(signal["price"]),
                tick_size,
                signal["side"],
            )
        return prepared

    def place_order(self, signal: dict) -> dict:
        """Place an order.

        Expected *signal* keys: ``symbol``, ``side`` (``"buy"``/``"sell"``),
        ``quantity``, ``order_type`` (``"market"``/``"limit"``),
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
        self._ib.disconnect()
        logger.info("IBKR disconnected")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_contract(
        self,
        symbol: str,
        *,
        security_type: str = "STK",
        exchange: str | None = None,
        currency: str = "USD",
        continuous_alias: bool = False,
        contract_month: str | None = None,
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
            raise ValueError(
                "continuous_alias and contract_month are valid only for IBKR futures"
            )

        cache_key = (symbol, security_type, exchange, currency, contract_month)
        if cache_key in self._contract_cache:
            cached = self._contract_cache[cache_key]
            if security_type != "FUT" or _future_contract_is_current(cached):
                return cached
            del self._contract_cache[cache_key]
            detail_cache = getattr(self, "_contract_details_cache", None)
            if detail_cache is not None:
                detail_cache.pop(cache_key, None)

        ib_async = _require_ib_async()

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
                month_matches = (
                    contract_month is None or raw_expiry[:6] == contract_month
                )
                if month_matches and expiry >= today:
                    unexpired_details.append((expiry, detail))
            if not unexpired_details:
                selection = (
                    f" contract_month={contract_month}"
                    if contract_month is not None
                    else ""
                )
                raise ValueError(
                    f"No non-expired future for {symbol} on {exchange}{selection}"
                )
            if contract_month is not None:
                candidates = unexpired_details
            else:
                nearest_expiry = min(item[0] for item in unexpired_details)
                candidates = [
                    item for item in unexpired_details if item[0] == nearest_expiry
                ]
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
            detail_cache = getattr(self, "_contract_details_cache", None)
            if detail_cache is None:
                detail_cache = self._contract_details_cache = {}
            detail_cache[cache_key] = selected

        self._contract_cache[cache_key] = resolved
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
    ):
        cache_key = (symbol, security_type, exchange, currency, contract_month)
        cache = getattr(self, "_contract_details_cache", None)
        if cache is None:
            cache = self._contract_details_cache = {}

        contract = self._resolve_contract(
            symbol,
            security_type=security_type,
            exchange=exchange,
            currency=currency,
            continuous_alias=continuous_alias,
            contract_month=contract_month,
        )
        if cache_key in cache:
            return cache[cache_key]
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
        cache[cache_key] = selected
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
