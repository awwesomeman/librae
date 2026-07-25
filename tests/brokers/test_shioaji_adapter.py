"""Tests for ShioajiAdapter.

All tests use mocks — no real Shioaji API calls.
Marked tw_live so they are skipped when shioaji is not installed.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

pytestmark = pytest.mark.tw_live


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(*, ca_activated: bool = False):
    """Build a ShioajiAdapter with mocked internals (no real login)."""
    from brokers.shioaji_adapter import ShioajiAdapter

    adapter = ShioajiAdapter.__new__(ShioajiAdapter)
    adapter._api = MagicMock()
    adapter._read_only = not ca_activated
    return adapter


def _make_kbars_response():
    """Simulate Shioaji kbars() return value (dict of lists).

    ``ts`` is raw nanoseconds, encoding Taipei wall-clock time as if it were
    a UTC epoch (Shioaji's actual behavior — see taipei_time.py) — these
    values correspond to Taipei local 09:00/09:05/09:10 on 2026-04-01, i.e.
    true UTC 01:00/01:05/01:10.
    """
    taipei_wall_clock = pd.to_datetime(
        ["2026-04-01 09:00", "2026-04-01 09:05", "2026-04-01 09:10"],
        utc=True,
    )
    return {
        # as_unit("ns") forces true nanosecond ints regardless of pandas's
        # default DatetimeIndex resolution — Shioaji's raw ts is always ns.
        "ts": taipei_wall_clock.as_unit("ns").astype("int64").tolist(),
        "open": [20100.0, 20120.0, 20150.0],
        "high": [20130.0, 20160.0, 20180.0],
        "low": [20080.0, 20100.0, 20140.0],
        "close": [20120.0, 20150.0, 20170.0],
        "volume": [500, 600, 450],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFetchOhlcv:
    def test_returns_correct_columns(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")

        df = adapter.fetch_ohlcv("TXFR1", "1m")

        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]
        assert len(df) == 3
        assert pd.api.types.is_datetime64_any_dtype(df["ts"])

    def test_corrects_taipei_epoch_offset(self):
        """Shioaji's raw ts is Taipei wall-clock as fake-UTC; must be shifted -8h."""
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")

        df = adapter.fetch_ohlcv("TXFR1", "1m")

        expected = pd.to_datetime(
            ["2026-04-01 01:00", "2026-04-01 01:05", "2026-04-01 01:10"],
            utc=True,
        )
        assert list(df["ts"]) == list(expected)

    def test_empty_response(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = {}
        adapter._resolve_contract = MagicMock(return_value="mock_contract")

        df = adapter.fetch_ohlcv("TXFR1", "1m")

        assert df.empty
        assert list(df.columns) == ["ts", "open", "high", "low", "close", "volume"]

    def test_limit_trims_tail(self):
        adapter = _make_adapter()
        adapter._api.kbars.return_value = _make_kbars_response()
        adapter._resolve_contract = MagicMock(return_value="mock_contract")

        df = adapter.fetch_ohlcv("TXFR1", "1m", limit=2)

        assert len(df) == 2


class TestReadOnlyGuard:
    def test_place_order_raises_without_ca(self):
        adapter = _make_adapter(ca_activated=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.place_order({"symbol": "TXFR1", "side": "buy", "quantity": 1})

    def test_get_position_raises_without_ca(self):
        adapter = _make_adapter(ca_activated=False)

        with pytest.raises(NotImplementedError, match="read-only"):
            adapter.get_position("TXFR1")


class TestPlaceOrder:
    """Exercises place_order()'s body against a mocked shioaji module — the
    previous test suite only covered the read-only guard (above), never the
    actual sj.Action/FuturesPriceType/OrderType construction, which is
    exactly how a real bug shipped: shioaji >=1.4 moved these enums from
    shioaji.order.* to top-level shioaji.*, and nothing caught it until a
    real order attempt against the live API."""

    def _mock_shioaji_module(self):
        mock_sj = MagicMock()
        mock_sj.Action.Buy = "BUY"
        mock_sj.Action.Sell = "SELL"
        mock_sj.FuturesPriceType.LMT = "FUT_LMT"
        mock_sj.FuturesPriceType.MKT = "FUT_MKT"
        mock_sj.StockPriceType.LMT = "STK_LMT"
        mock_sj.StockPriceType.MKT = "STK_MKT"
        mock_sj.OrderType.ROD = "ROD"
        mock_sj.OrderType.IOC = "IOC"
        return mock_sj

    def test_futures_limit_order_uses_futures_price_type(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._is_futures = MagicMock(return_value=True)
        mock_trade = MagicMock()
        mock_trade.status.id = "order123"
        mock_trade.status.status = "PendingSubmit"
        adapter._api.place_order.return_value = mock_trade
        adapter._api.Order = MagicMock(return_value="mock_order")

        with patch(
            "brokers.shioaji_adapter._require_shioaji", return_value=self._mock_shioaji_module()
        ):
            result = adapter.place_order(
                {
                    "symbol": "TXFR1",
                    "side": "buy",
                    "quantity": 1,
                    "order_type": "limit",
                    "price": 17000,
                }
            )

        adapter._api.Order.assert_called_once_with(
            price=17000,
            quantity=1,
            action="BUY",
            price_type="FUT_LMT",
            order_type="ROD",
        )
        assert result == {"id": "order123", "status": "PendingSubmit"}

    def test_stock_market_order_uses_stock_price_type(self):
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._is_futures = MagicMock(return_value=False)
        mock_trade = MagicMock()
        mock_trade.status.id = "order456"
        mock_trade.status.status = "Filled"
        adapter._api.place_order.return_value = mock_trade
        adapter._api.Order = MagicMock(return_value="mock_order")

        with patch(
            "brokers.shioaji_adapter._require_shioaji", return_value=self._mock_shioaji_module()
        ):
            adapter.place_order({"symbol": "2330", "side": "sell", "quantity": 1000})

        adapter._api.Order.assert_called_once_with(
            price=0,
            quantity=1000,
            action="SELL",
            price_type="STK_MKT",
            order_type="IOC",
        )

    def test_futures_market_order_uses_ioc(self):
        """TAIFEX rejects market orders with ROD time-in-force (op_code 9938:
        "市價單不允許當日有效委託(ROD)") -- caught live 2026-07-20. Market orders
        must be IOC; only resting limit orders may use ROD."""
        adapter = _make_adapter(ca_activated=True)
        adapter._resolve_contract = MagicMock(return_value="mock_contract")
        adapter._is_futures = MagicMock(return_value=True)
        mock_trade = MagicMock()
        mock_trade.status.id = "order789"
        mock_trade.status.status = "Filled"
        adapter._api.place_order.return_value = mock_trade
        adapter._api.Order = MagicMock(return_value="mock_order")

        with patch(
            "brokers.shioaji_adapter._require_shioaji", return_value=self._mock_shioaji_module()
        ):
            adapter.place_order({"symbol": "TMFR1", "side": "buy", "quantity": 1})

        adapter._api.Order.assert_called_once_with(
            price=0,
            quantity=1,
            action="BUY",
            price_type="FUT_MKT",
            order_type="IOC",
        )


class TestResolveContract:
    def test_futures_found(self):
        adapter = _make_adapter()
        mock_contract = MagicMock()
        adapter._api.Contracts.Futures.get.return_value = mock_contract

        result = adapter._resolve_contract("TXFR1")

        assert result is mock_contract
        adapter._api.Contracts.Futures.get.assert_called_once_with("TXFR1")

    def test_stocks_fallback(self):
        adapter = _make_adapter()
        adapter._api.Contracts.Futures.get.return_value = None
        mock_contract = MagicMock()
        adapter._api.Contracts.Stocks.get.return_value = mock_contract

        result = adapter._resolve_contract("2330")

        assert result is mock_contract

    def test_unknown_symbol_raises(self):
        adapter = _make_adapter()
        adapter._api.Contracts.Futures.get.return_value = None
        adapter._api.Contracts.Stocks.get.return_value = None

        with pytest.raises(ValueError, match="Unknown symbol"):
            adapter._resolve_contract("INVALID")


class TestLifecycle:
    def test_close_calls_logout(self):
        adapter = _make_adapter()
        adapter.close()
        adapter._api.logout.assert_called_once()

    def test_context_manager(self):
        adapter = _make_adapter()
        with adapter:
            pass
        adapter._api.logout.assert_called_once()


class TestInit:
    """ShioajiAdapter.__init__ itself — the fixtures above all bypass it via
    __new__, so login/CA-activation/read_only were never actually tested."""

    def _mock_sj(self, mock_api: MagicMock) -> MagicMock:
        mock_sj = MagicMock()
        mock_sj.Shioaji.return_value = mock_api
        return mock_sj

    def test_missing_credentials_raises(self):
        from brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        with (
            patch("brokers.shioaji_adapter._require_shioaji"),
            pytest.raises(ValueError, match="credentials"),
        ):
            ShioajiAdapter(credentials=ShioajiCredentials(api_key="", secret_key=""))

    def test_login_without_ca_path_is_read_only(self):
        from brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        with patch(
            "brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s", person_id="p"),
            )
        mock_api.login.assert_called_once_with(api_key="k", secret_key="s")
        mock_api.activate_ca.assert_not_called()
        assert adapter._read_only is True

    def test_login_with_ca_path_enables_trading(self):
        from brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        with patch(
            "brokers.shioaji_adapter._require_shioaji", return_value=self._mock_sj(mock_api)
        ):
            adapter = ShioajiAdapter(
                credentials=ShioajiCredentials(
                    api_key="k",
                    secret_key="s",
                    person_id="p",
                    ca_path="/path/to/ca",
                    ca_password="pw",
                ),
            )
        mock_api.activate_ca.assert_called_once_with(
            ca_path="/path/to/ca", ca_passwd="pw", person_id="p"
        )
        assert adapter._read_only is False

    def test_simulation_flag_passed_through(self):
        from brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        mock_sj = self._mock_sj(mock_api)
        with patch("brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s"), simulation=True
            )
        mock_sj.Shioaji.assert_called_once_with(simulation=True)

    def test_credentials_sandbox_overrides_simulation_param(self):
        """ShioajiCredentials.sandbox (SHIOAJI_SANDBOX) takes precedence,
        mirroring CryptoCredentials.sandbox — orthogonal to RunConfig.mode,
        deliberately not derived from it (see librae/live/engine.py)."""
        from brokers.shioaji_adapter import ShioajiAdapter, ShioajiCredentials

        mock_api = MagicMock()
        mock_sj = self._mock_sj(mock_api)
        with patch("brokers.shioaji_adapter._require_shioaji", return_value=mock_sj):
            ShioajiAdapter(
                credentials=ShioajiCredentials(api_key="k", secret_key="s", sandbox=True),
                simulation=False,
            )
        mock_sj.Shioaji.assert_called_once_with(simulation=True)

    def test_sandbox_string_env_value_coerced_to_bool(self):
        from brokers.shioaji_adapter import ShioajiCredentials

        creds = ShioajiCredentials(api_key="k", secret_key="s", sandbox="true")
        assert creds.sandbox is True
