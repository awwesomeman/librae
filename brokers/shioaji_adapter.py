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
from datetime import datetime, timezone

import pandas as pd

from .base import AdapterInfo, CredentialConfig

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


class ShioajiAdapter:
    """Taiwan futures/stocks adapter backed by Shioaji SDK.

    Parameters
    ----------
    credentials : ShioajiCredentials | None
        If None, loads from env vars with ``SHIOAJI_`` prefix.
    simulation : bool
        If True, use Shioaji simulation mode (paper trading).

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

        if not creds.api_key or not creds.secret_key:
            raise ValueError(
                "Shioaji requires API credentials for all operations. "
                "Set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY env vars, "
                "or pass ShioajiCredentials explicitly."
            )

        self._api = sj.Shioaji(simulation=simulation)
        self._api.login(
            api_key=creds.api_key,
            secret_key=creds.secret_key,
            person_id=creds.person_id,
        )
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
        timeframe: str = "1min",
        *,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        limit: int = 200,
    ) -> pd.DataFrame:
        """Fetch OHLCV via Shioaji kbars API.

        Shioaji kbars always returns 1-min bars. When *timeframe* is
        coarser (e.g. ``"5min"``, ``"1D"``), the result is resampled
        using ``data.utils.resample_ohlcv``.

        Args:
            symbol: Contract code (e.g. ``"TXFR1"``, ``"2330"``).
            timeframe: Target candle interval as pandas resample rule
                (``"1min"``, ``"5min"``, ``"30min"``, ``"60min"``, ``"1D"``).
            start/end: Date range as datetime or ``"YYYY-MM-DD"`` string.
                If omitted, fetches the most recent *limit* bars.
            limit: Max bars (used only when start/end are omitted).

        Returns columns: ``[ts, open, high, low, close, volume]``
        where ``ts`` is a UTC-aware datetime.
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
        df["ts"] = pd.to_datetime(df["ts"], utc=True)

        # Resample if requested timeframe is coarser than 1-min
        if timeframe not in ("1min", "1T"):
            from data.utils import resample_ohlcv
            df = df.set_index("ts")
            df = resample_ohlcv(df, rule=timeframe)
            df = df.reset_index().rename(columns={df.index.name or "index": "ts"})

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

        action = (
            sj.order.Action.Buy if signal["side"] == "buy"
            else sj.order.Action.Sell
        )
        is_limit = signal.get("order_type") == "limit"
        if is_futures:
            price_type = sj.order.FuturesPriceType.LMT if is_limit else sj.order.FuturesPriceType.MKT
        else:
            price_type = sj.order.StockPriceType.LMT if is_limit else sj.order.StockPriceType.MKT

        order = self._api.Order(
            price=signal.get("price", 0),
            quantity=int(signal["quantity"]),
            action=action,
            price_type=price_type,
            order_type=sj.order.OrderType.ROD,
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
