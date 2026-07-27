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
from numbers import Real

import pandas as pd

from .base import AdapterInfo, CredentialConfig, find_position
from .taipei_time import resample_taifex_ohlcv, shioaji_ts_ns_to_epoch

logger = logging.getLogger(__name__)


def _require_shioaji():
    """Import and return shioaji, raising a friendly error if missing."""
    try:
        import shioaji

        return shioaji
    except ImportError as e:
        raise ImportError(
            "shioaji is required for ShioajiAdapter. "
            "Install it with: pip install shioaji  "
            "or: pip install -e '.[tw-live]'"
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
    ) -> pd.DataFrame:
        """Fetch OHLCV via Shioaji kbars API.

        Shioaji kbars always returns 1-min bars. When *timeframe* is coarser,
        the result is resampled on TAIFEX session/trading-day boundaries
        (``taipei_time.resample_taifex_ohlcv``) — not a plain UTC-epoch grid,
        which would mislabel bars around session opens/closes.

        Shioaji's raw kbar ``ts`` encodes Taipei wall-clock time as if it
        were a UTC epoch; this is corrected to a true UTC epoch before
        returning (``taipei_time.shioaji_ts_ns_to_epoch``).

        Args:
            symbol: Contract code (e.g. ``"TXFR1"``, ``"2330"``).
            timeframe: Target candle interval, ccxt or canonical format
                (e.g. ``"1m"``, ``"5m"``, ``"30m"``, ``"1h"``, ``"1d"``) —
                same convention as CryptoAdapter.
            start/end: Date range as datetime or ``"YYYY-MM-DD"`` string.
                If omitted, fetches the most recent *limit* bars.
            limit: Max bars (used only when start/end are omitted).

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a true UTC-aware datetime.
        """
        contract = self._resolve_contract(symbol)

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
            df = resample_taifex_ohlcv(df.set_index("ts"), target_seconds).reset_index()

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

        sj = _require_shioaji()
        contract = self._resolve_contract(signal["symbol"])
        is_futures = self._is_futures(signal["symbol"])

        # shioaji >=1.4 moved these enums from shioaji.order.* to top-level
        # shioaji.* (member names unchanged) — sj.order.Action etc. raises
        # AttributeError on current versions.
        action = sj.Action.Buy if signal["side"] == "buy" else sj.Action.Sell
        is_limit = signal.get("order_type") == "limit"
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
        order_kwargs = dict(
            price=signal.get("price", 0),
            quantity=int(signal["quantity"]),
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
        contract_code = str(self._resolve_contract(symbol).code)
        self._api.update_status()
        return [
            trade
            for trade in self._api.list_trades()
            if str(getattr(trade.contract, "code", "")) == contract_code
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

    def get_position(self, symbol: str) -> dict:
        """Return current position for *symbol*."""
        self._require_auth()
        contract_code = str(self._resolve_contract(symbol).code)
        positions = self._api.list_positions()
        return find_position(
            positions,
            symbol,
            matches=lambda p: str(p.code) == contract_code,
            size=lambda p: p.quantity,
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
            return {"free": 0.0, "used": 0.0, "total": 0.0}
        margin = self._api.margin()
        total = float(getattr(margin, "equity", 0) or 0)
        free = float(getattr(margin, "available_margin", 0) or 0)
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

    def _is_futures(self, symbol: str) -> bool:
        """Check if a symbol is a futures contract."""
        contract = self._api.contracts.get(symbol)
        return contract is not None and contract.security_type == "FUT"


def _to_date_str(dt: datetime | str) -> str:
    """Convert datetime or string to 'YYYY-MM-DD' format."""
    if isinstance(dt, str):
        return dt[:10]
    return dt.strftime("%Y-%m-%d")
