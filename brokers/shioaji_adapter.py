"""ShioajiAdapter — Sinopac Shioaji adapter for Taiwan futures/stocks.

Wraps Shioaji SDK to provide the same duck-typed interface as CryptoAdapter:
``fetch_ohlcv``, ``place_order``, ``get_position``.

Authentication is **required** for all operations (including market data).
Order placement additionally requires CA certificate activation.

Credentials can be passed explicitly or loaded from environment variables
using the ``SHIOAJI_`` prefix convention::

    SHIOAJI_API_KEY, SHIOAJI_SECRET_KEY, SHIOAJI_PERSON_ID, SHIOAJI_CA_PATH

Install: ``pip install shioaji`` or ``pip install -e '.[tw-live]'``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from .base import AdapterInfo, CredentialConfig
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
        """
        self._require_auth()

        sj = _require_shioaji()
        contract = self._resolve_contract(signal["symbol"])
        is_futures = self._is_futures(signal["symbol"])

        # shioaji >=1.4 moved these enums from shioaji.order.* to top-level
        # shioaji.* (member names unchanged) — sj.order.Action etc. raises
        # AttributeError on current versions.
        action = (
            sj.Action.Buy if signal["side"] == "buy"
            else sj.Action.Sell
        )
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

        order = self._api.Order(
            price=signal.get("price", 0),
            quantity=int(signal["quantity"]),
            action=action,
            price_type=price_type,
            order_type=order_type,
        )
        trade = self._api.place_order(contract, order)
        return {
            "id": trade.status.id if trade.status else "",
            "status": trade.status.status if trade.status else "unknown",
        }

    def get_position(self, symbol: str) -> dict:
        """Return current position for *symbol*."""
        self._require_auth()

        positions = self._api.list_positions()
        for pos in positions:
            if pos.code == symbol:
                return {
                    "symbol": symbol,
                    "size": pos.quantity,
                    "avg_price": pos.price,
                    "unrealized_pnl": getattr(pos, "pnl", 0),
                }
        return {"symbol": symbol, "size": 0, "avg_price": 0, "unrealized_pnl": 0}

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
        contract = self._api.Contracts.Futures.get(symbol)
        if contract is None:
            contract = self._api.Contracts.Stocks.get(symbol)
        if contract is None:
            raise ValueError(f"Unknown symbol: {symbol}")
        return contract

    def _is_futures(self, symbol: str) -> bool:
        """Check if a symbol is a futures contract."""
        return self._api.Contracts.Futures.get(symbol) is not None


def _to_date_str(dt: datetime | str) -> str:
    """Convert datetime or string to 'YYYY-MM-DD' format."""
    if isinstance(dt, str):
        return dt[:10]
    return dt.strftime("%Y-%m-%d")
