"""Stateless contracts for supervising independent account runs.

Concrete process managers own lifecycle state. This module only describes one
deployment, its observable status, and the operations a deployment adapter
must provide.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

from librae.core.run_config import LiveMode

type LifecyclePhase = Literal[
    "stopped",
    "starting",
    "running",
    "stopping",
    "failed",
    "unknown",
]


def _require_non_empty(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True, slots=True)
class DeploymentSpec:
    """Stable identity and launch facts for one account-specific process."""

    deployment_id: str
    account_id: str
    currency: str
    mode: LiveMode
    strategy_name: str
    config_ref: str
    entrypoint: str
    credentials_ref: str | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.deployment_id, "deployment_id"),
            (self.account_id, "account_id"),
            (self.currency, "currency"),
            (self.strategy_name, "strategy_name"),
            (self.config_ref, "config_ref"),
            (self.entrypoint, "entrypoint"),
        ):
            _require_non_empty(value, field_name)
        if self.mode not in ("sim", "live"):
            raise ValueError("mode must be 'sim' or 'live'")
        if self.mode == "live":
            _require_non_empty(self.credentials_ref, "credentials_ref")


@dataclass(frozen=True, slots=True)
class DeploymentStatus:
    """Supervisor-observed facts for one deployment."""

    deployment_id: str
    account_id: str
    currency: str
    phase: LifecyclePhase
    observed_at: datetime
    run_id: str | None = None
    process_id: str | None = None
    exit_code: int | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.deployment_id, "deployment_id"),
            (self.account_id, "account_id"),
            (self.currency, "currency"),
        ):
            _require_non_empty(value, field_name)
        if self.phase not in (
            "stopped",
            "starting",
            "running",
            "stopping",
            "failed",
            "unknown",
        ):
            raise ValueError(f"unsupported lifecycle phase: {self.phase!r}")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        object.__setattr__(self, "observed_at", self.observed_at.astimezone(UTC))


class Supervisor(Protocol):
    """Adapter implemented by Docker, systemd, Kubernetes, or another supervisor."""

    def start(self, spec: DeploymentSpec) -> DeploymentStatus: ...

    def stop(self, deployment_id: str, *, force: bool = False) -> DeploymentStatus: ...

    def inspect(self, deployment_id: str) -> DeploymentStatus: ...

    def restart(self, deployment_id: str) -> DeploymentStatus: ...


def validate_deployments(deployments: Iterable[DeploymentSpec]) -> tuple[DeploymentSpec, ...]:
    """Reject ambiguous deployment or live-account ownership."""

    resolved = tuple(deployments)
    deployment_ids: set[str] = set()
    live_account_ids: set[str] = set()
    for deployment in resolved:
        if deployment.deployment_id in deployment_ids:
            raise ValueError(f"duplicate deployment_id: {deployment.deployment_id!r}")
        deployment_ids.add(deployment.deployment_id)

        if deployment.mode != "live":
            continue
        if deployment.account_id in live_account_ids:
            raise ValueError(f"duplicate live account_id: {deployment.account_id!r}")
        live_account_ids.add(deployment.account_id)
    return resolved
