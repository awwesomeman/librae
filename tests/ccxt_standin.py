"""A ccxt stand-in for building a real ``CryptoAdapter`` without the SDK.

Its error classes copy ccxt's hierarchy for every class the tests raise, so
classification runs in the ccxt-free job. The sdk_contract suite checks the
copy against the installed ccxt.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from librae.brokers.crypto_adapter import CryptoAdapter

# Each class and its parent, as ccxt/base/errors.py defines them; a parent
# comes before its children.
_PARENTS: dict[str, str | None] = {
    "BaseError": None,
    "ExchangeError": "BaseError",
    "AuthenticationError": "ExchangeError",
    "PermissionDenied": "AuthenticationError",
    "AccountNotEnabled": "PermissionDenied",
    "AccountSuspended": "AuthenticationError",
    "ArgumentsRequired": "ExchangeError",
    "BadRequest": "ExchangeError",
    "BadSymbol": "BadRequest",
    "OperationRejected": "ExchangeError",
    "NoChange": "OperationRejected",
    "MarketClosed": "OperationRejected",
    "ManualInteractionNeeded": "OperationRejected",
    "RestrictedLocation": "OperationRejected",
    "InsufficientFunds": "ExchangeError",
    "InvalidOrder": "ExchangeError",
    "OrderNotFound": "InvalidOrder",
    "OrderNotCached": "InvalidOrder",
    "OrderImmediatelyFillable": "InvalidOrder",
    "OrderNotFillable": "InvalidOrder",
    "DuplicateOrderId": "InvalidOrder",
    "ContractUnavailable": "InvalidOrder",
    "NotSupported": "ExchangeError",
    "OperationFailed": "BaseError",
    "NetworkError": "OperationFailed",
    "DDoSProtection": "NetworkError",
    "RateLimitExceeded": "NetworkError",
    "ExchangeNotAvailable": "NetworkError",
    "OnMaintenance": "ExchangeNotAvailable",
    "InvalidNonce": "NetworkError",
    "ChecksumError": "InvalidNonce",
    "RequestTimeout": "NetworkError",
    "BadResponse": "OperationFailed",
}

_classes: dict[str, type[Exception]] = {}
for _name, _parent in _PARENTS.items():
    _classes[_name] = type(_name, (Exception if _parent is None else _classes[_parent],), {})

errors = SimpleNamespace(**_classes)
PRECISION_MODES = {"TICK_SIZE": 4, "DECIMAL_PLACES": 2}


def build_crypto_adapter(exchange: object, exchange_id: str = "binance", **kwargs) -> CryptoAdapter:
    """Build a ``CryptoAdapter`` through ``__init__`` whose exchange is ``exchange``."""
    ccxt = SimpleNamespace(**PRECISION_MODES, **_classes, **{exchange_id: lambda _: exchange})
    with patch("librae.brokers.crypto_adapter._require_ccxt", return_value=ccxt):
        return CryptoAdapter(exchange_id=exchange_id, **kwargs)
