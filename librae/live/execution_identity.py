"""Non-secret identity for one authenticated broker execution route."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast

# Two of the three values mean "not production", and which one an adapter
# reports follows the venue's own vocabulary rather than a librae distinction:
# CCXT testnets report "sandbox", broker paper accounts (IBKR, Shioaji
# simulation) report "paper". Test for `!= "production"` when the question is
# whether real money is at risk; `== "paper"` silently excludes every testnet.
ExecutionEnvironment = Literal["sandbox", "paper", "production"]
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:-]+$")
_FINGERPRINT = re.compile(r"^[0-9a-f]{16,64}$")


def account_fingerprint(broker: str, *observed_parts: str) -> str:
    """Hash adapter-observed account facts without retaining their raw values."""
    normalized = [str(part).strip() for part in observed_parts if str(part).strip()]
    if not normalized:
        raise ValueError(f"{broker} did not expose an authenticated account identity")
    payload = json.dumps([broker, *sorted(normalized)], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ExecutionIdentity:
    """Sanitized broker environment identity safe for logs and persistence."""

    broker: str
    environment: ExecutionEnvironment
    endpoint: str
    account_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("broker", "endpoint"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
                raise ValueError(f"execution identity {name} must be a non-secret label")
        if self.environment not in ("sandbox", "paper", "production"):
            raise ValueError(f"invalid execution environment: {self.environment!r}")
        if not _FINGERPRINT.fullmatch(self.account_fingerprint):
            raise ValueError("execution account_fingerprint must be an opaque lowercase hex digest")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ExecutionIdentity:
        expected = {"broker", "environment", "endpoint", "account_fingerprint"}
        if set(raw) != expected or not all(isinstance(raw[key], str) for key in expected):
            raise ValueError("malformed execution identity")
        return cls(
            broker=str(raw["broker"]),
            environment=cast(ExecutionEnvironment, raw["environment"]),
            endpoint=str(raw["endpoint"]),
            account_fingerprint=str(raw["account_fingerprint"]),
        )

    @property
    def key_digest(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @property
    def summary(self) -> str:
        return (
            f"broker={self.broker} environment={self.environment} endpoint={self.endpoint} "
            f"account={self.account_fingerprint}"
        )


class ExecutionIdentityProvider(Protocol):
    """Order-adapter capability resolved before live state restoration."""

    def execution_identity(self) -> ExecutionIdentity: ...


def runtime_state_key(
    mode: str,
    config_hash: str,
    execution_identity: ExecutionIdentity | None = None,
) -> str:
    if mode == "live":
        if execution_identity is None:
            raise ValueError("live runtime state key requires an execution_identity")
        return f"live:{config_hash}:{execution_identity.key_digest}"
    if execution_identity is not None:
        raise ValueError("non-live runtime state key cannot include an execution_identity")
    return f"{mode}:{config_hash}"


def account_lease_key(identity: ExecutionIdentity) -> str:
    return (
        f"live-account:{identity.broker}:{identity.environment}:"
        f"{identity.endpoint}:{identity.account_fingerprint}"
    )


def resolve_execution_identity(provider: object) -> ExecutionIdentity:
    """Read and validate the capability before checkpoint or order access."""
    callback = getattr(provider, "execution_identity", None)
    if not callable(callback):
        raise RuntimeError(
            "live order adapter must expose execution_identity() with its authenticated "
            "broker environment and account fingerprint"
        )
    try:
        identity = callback()
    except Exception as exc:
        raise RuntimeError("live execution identity is unavailable; trading did not start") from exc
    if not isinstance(identity, ExecutionIdentity):
        raise RuntimeError("live order adapter returned an invalid execution identity")
    return identity
