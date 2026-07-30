"""Docker CLI reference adapter for account-specific deployments."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from librae.orchestration.supervisor import (
    DeploymentSpec,
    DeploymentStatus,
    LifecyclePhase,
)


class DockerSupervisor:
    """Delegate lifecycle state to Docker through the reference ``trade.sh``."""

    def __init__(
        self,
        trade_script: str | Path,
        *,
        poll_seconds: int = 60,
        environment: Mapping[str, str] | None = None,
        command_prefix: Sequence[str] = ("bash",),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, int) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be a positive integer")
        if not command_prefix:
            raise ValueError("command_prefix must not be empty")
        self._command = (*command_prefix, str(trade_script))
        self._poll_seconds = poll_seconds
        self._environment = dict(environment or {})
        self._clock = clock

    def _invoke(self, *arguments: str) -> str:
        environment = os.environ.copy()
        environment.update(self._environment)
        result = subprocess.run(
            [*self._command, *arguments],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise RuntimeError(f"trade supervisor command failed: {detail}")
        return result.stdout

    @staticmethod
    def _parse_status(output: str) -> dict[str, str]:
        facts: dict[str, str] = {}
        for line in output.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                facts[key] = value
        required = {"deployment_id", "account_id", "currency", "phase"}
        missing = required - facts.keys()
        if missing:
            raise RuntimeError(f"trade supervisor omitted status fields: {sorted(missing)}")
        return facts

    def _status(self, deployment_id: str) -> DeploymentStatus:
        facts = self._parse_status(self._invoke("inspect", deployment_id))
        if facts["deployment_id"] != deployment_id:
            raise RuntimeError(
                f"trade supervisor returned a different deployment_id: {facts['deployment_id']!r}"
            )
        phase = cast(LifecyclePhase, facts["phase"])
        exit_code = facts.get("exit_code")
        reason = facts.get("reason") or None
        restart_count = facts.get("restart_count", "0")
        if phase == "failed" and reason is None:
            reason = f"container failed after {restart_count} restart(s)"
        return DeploymentStatus(
            deployment_id=deployment_id,
            account_id=facts["account_id"],
            currency=facts["currency"],
            phase=phase,
            observed_at=self._clock(),
            run_id=facts.get("run_id") or None,
            process_id=facts.get("process_id") or None,
            exit_code=int(exit_code) if exit_code else None,
            reason=reason,
        )

    def start(self, spec: DeploymentSpec) -> DeploymentStatus:
        expected_entrypoint = f"strategies.{spec.strategy_name}.run"
        if spec.entrypoint != expected_entrypoint:
            raise ValueError(
                f"DockerSupervisor requires entrypoint={expected_entrypoint!r}, "
                f"got {spec.entrypoint!r}"
            )
        arguments = [
            "start",
            spec.deployment_id,
            spec.account_id,
            spec.currency,
            spec.strategy_name,
            spec.mode,
            str(self._poll_seconds),
            "--config",
            spec.config_ref,
        ]
        if spec.credentials_ref is not None:
            arguments.extend(("--credentials", spec.credentials_ref))
        self._invoke(*arguments)
        return self._status(spec.deployment_id)

    def stop(self, deployment_id: str, *, force: bool = False) -> DeploymentStatus:
        arguments = ["stop", deployment_id]
        if force:
            arguments.append("--force")
        self._invoke(*arguments)
        return self._status(deployment_id)

    def inspect(self, deployment_id: str) -> DeploymentStatus:
        return self._status(deployment_id)

    def restart(self, deployment_id: str) -> DeploymentStatus:
        self._invoke("restart", deployment_id)
        return self._status(deployment_id)
