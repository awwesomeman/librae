"""Tests for external supervisor contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from librae.orchestration.supervisor import (
    DeploymentSpec,
    DeploymentStatus,
    Supervisor,
    validate_deployments,
)


def _deployment(number: int, *, account_id: str | None = None) -> DeploymentSpec:
    resolved_account_id = account_id or f"account-{number}"
    return DeploymentSpec(
        deployment_id=f"momentum-{number}",
        account_id=resolved_account_id,
        currency="USD",
        mode="live",
        strategy_name="momentum",
        config_ref=f"configs/account-{number}.yaml",
        entrypoint="strategies.momentum.run",
        credentials_ref=f"secrets/account-{number}",
    )


class _FakeSupervisor:
    def __init__(self, processes: dict[str, DeploymentStatus] | None = None) -> None:
        self.processes = processes if processes is not None else {}

    def start(self, spec: DeploymentSpec) -> DeploymentStatus:
        status = DeploymentStatus(
            deployment_id=spec.deployment_id,
            account_id=spec.account_id,
            currency=spec.currency,
            phase="running",
            observed_at=datetime.now(UTC),
            run_id=f"run-{spec.deployment_id}",
            process_id=f"process-{spec.deployment_id}",
        )
        self.processes[spec.deployment_id] = status
        return status

    def stop(self, deployment_id: str, *, force: bool = False) -> DeploymentStatus:
        current = self.inspect(deployment_id)
        status = DeploymentStatus(
            deployment_id=current.deployment_id,
            account_id=current.account_id,
            currency=current.currency,
            phase="stopped",
            observed_at=datetime.now(UTC),
            run_id=current.run_id,
            exit_code=-9 if force else 0,
        )
        self.processes[deployment_id] = status
        return status

    def inspect(self, deployment_id: str) -> DeploymentStatus:
        return self.processes[deployment_id]

    def restart(self, deployment_id: str) -> DeploymentStatus:
        current = self.inspect(deployment_id)
        status = DeploymentStatus(
            deployment_id=current.deployment_id,
            account_id=current.account_id,
            currency=current.currency,
            phase="running",
            observed_at=datetime.now(UTC),
            run_id=current.run_id,
            process_id=f"restarted-{deployment_id}",
        )
        self.processes[deployment_id] = status
        return status


def _start(supervisor: Supervisor, spec: DeploymentSpec) -> DeploymentStatus:
    return supervisor.start(spec)


def test_three_account_runs_keep_independent_identity_and_state() -> None:
    specs = validate_deployments(_deployment(number) for number in range(1, 4))
    process_state: dict[str, DeploymentStatus] = {}
    supervisor = _FakeSupervisor(process_state)

    statuses = [_start(supervisor, spec) for spec in specs]
    supervisor.stop(specs[1].deployment_id)

    reconnected_supervisor = _FakeSupervisor(process_state)
    assert {status.account_id for status in statuses} == {
        "account-1",
        "account-2",
        "account-3",
    }
    assert reconnected_supervisor.inspect(specs[0].deployment_id).phase == "running"
    assert reconnected_supervisor.inspect(specs[1].deployment_id).phase == "stopped"
    assert reconnected_supervisor.inspect(specs[2].deployment_id).phase == "running"


def test_failed_run_does_not_block_other_deployments() -> None:
    first, failed, third = validate_deployments(_deployment(number) for number in range(1, 4))
    supervisor = _FakeSupervisor()
    for spec in (first, failed, third):
        supervisor.start(spec)
    supervisor.processes[failed.deployment_id] = DeploymentStatus(
        deployment_id=failed.deployment_id,
        account_id=failed.account_id,
        currency=failed.currency,
        phase="failed",
        observed_at=datetime.now(UTC),
        exit_code=1,
        reason="process exited",
    )

    restarted = supervisor.restart(third.deployment_id)

    assert supervisor.inspect(first.deployment_id).phase == "running"
    assert supervisor.inspect(failed.deployment_id).phase == "failed"
    assert restarted.phase == "running"


def test_manifest_rejects_duplicate_deployment_identity() -> None:
    spec = _deployment(1)

    with pytest.raises(ValueError, match="duplicate deployment_id"):
        validate_deployments([spec, spec])


def test_manifest_rejects_duplicate_live_account_ownership() -> None:
    with pytest.raises(ValueError, match="duplicate live account_id"):
        validate_deployments(
            [
                _deployment(1, account_id="shared"),
                _deployment(2, account_id="shared"),
            ]
        )


def test_live_deployment_requires_account_specific_credentials() -> None:
    with pytest.raises(ValueError, match="credentials_ref"):
        DeploymentSpec(
            deployment_id="momentum-1",
            account_id="account-1",
            currency="USD",
            mode="live",
            strategy_name="momentum",
            config_ref="configs/account-1.yaml",
            entrypoint="strategies.momentum.run",
        )


def test_status_requires_timezone_aware_observation() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        DeploymentStatus(
            deployment_id="momentum-1",
            account_id="account-1",
            currency="USD",
            phase="unknown",
            observed_at=datetime.now(),
        )
