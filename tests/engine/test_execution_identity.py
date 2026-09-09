from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from librae.live.execution_identity import (
    ExecutionIdentity,
    account_fingerprint,
    resolve_execution_identity,
    runtime_state_key,
)


def _identity(**overrides: str) -> ExecutionIdentity:
    values = {
        "broker": "ibkr",
        "environment": "paper",
        "endpoint": "gateway:7497",
        "account_fingerprint": "a" * 24,
        **overrides,
    }
    return ExecutionIdentity(**values)


def test_raw_account_identifier_never_reaches_identity_or_summary() -> None:
    raw_account = "DU1234567"
    identity = _identity(account_fingerprint=account_fingerprint("ibkr", raw_account))

    assert raw_account not in identity.account_fingerprint
    assert raw_account not in identity.summary


@pytest.mark.parametrize(
    "changed",
    [
        {"environment": "production"},
        {"endpoint": "gateway:7496"},
        {"account_fingerprint": "b" * 24},
    ],
)
def test_checkpoint_key_changes_across_execution_boundaries(changed: dict[str, str]) -> None:
    config_hash = "config-a"

    assert runtime_state_key("live", config_hash, _identity()) != runtime_state_key(
        "live", config_hash, _identity(**changed)
    )


def test_live_checkpoint_key_requires_identity() -> None:
    with pytest.raises(ValueError, match="requires an execution_identity"):
        runtime_state_key("live", "config-a")


def test_adapter_identity_capability_fails_closed() -> None:
    adapter = MagicMock()
    adapter.execution_identity.return_value = None

    with pytest.raises(RuntimeError, match="invalid execution identity"):
        resolve_execution_identity(adapter)


def test_identity_rejects_endpoint_that_could_contain_credentials() -> None:
    with pytest.raises(ValueError, match="non-secret label"):
        _identity(endpoint="https://user:secret@example.test")
