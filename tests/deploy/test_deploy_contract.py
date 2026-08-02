"""Deployment contract checks that do not require a Docker daemon."""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


def _find_bash() -> str:
    candidates: list[Path] = []
    bash = shutil.which("bash")
    if bash is not None:
        candidates.append(Path(bash))
    if os.name == "nt":
        git = shutil.which("git")
        if git is not None:
            candidates.append(Path(git).resolve().parent.parent / "bin/bash.exe")
        candidates.append(
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git/bin/bash.exe"
        )

    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "exit 0"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return str(candidate)

    pytest.skip("a working bash is required for deployment script behavior tests")


def test_compose_database_init_mount_exists() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    mounts = compose["services"]["timescaledb"]["volumes"]
    init_mount = next(mount for mount in mounts if "docker-entrypoint-initdb.d" in mount)
    source = init_mount.split(":", maxsplit=1)[0]

    assert (DEPLOY / source).resolve() == (ROOT / "librae" / "db" / "timescale_init.sql").resolve()
    assert (DEPLOY / source).is_file()


def test_trade_image_installs_every_supported_runtime_extra() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (DEPLOY / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_project = next(package for package in lock["package"] if package["name"] == "librae")
    runtime_extras = (
        "calendars",
        "cli",
        "db",
        "crypto-live",
        "telegram",
        "tw-live",
        "us-live",
    )

    assert "uv sync --locked --no-dev --no-editable --no-cache" in dockerfile
    assert "--no-python-downloads" in dockerfile
    assert "--mount=type=secret,id=uv_config" in dockerfile
    assert "UV_CONFIG_FILE=/run/secrets/uv_config" in dockerfile
    assert "pip install" not in dockerfile
    for extra in runtime_extras:
        assert f"--extra {extra}" in dockerfile
        assert extra in project["project"]["optional-dependencies"]
        assert extra in locked_project["optional-dependencies"]

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    python_bases = [line.split()[1] for line in from_lines if "python:3.12-slim@" in line]
    assert len(python_bases) == 2
    assert len(set(python_bases)) == 1
    assert python_bases[0].startswith("python:3.12-slim@sha256:")
    assert len(python_bases[0].partition("@sha256:")[2]) == 64
    assert any(
        line.startswith("FROM ghcr.io/astral-sh/uv:") and "@sha256:" in line for line in from_lines
    )

    assert "COPY --from=strategy_source . strategies/" in dockerfile
    assert "COPY --from=build /app/.venv .venv/" in dockerfile
    assert "COPY --from=build /app/uv.lock uv.lock" in dockerfile
    assert "COPY orchestration_helpers.py" not in dockerfile
    assert "**/.git" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "**/.secrets" in dockerignore
    assert "\ntests\n" in dockerignore
    assert "\ndocs\n" in dockerignore


def test_trade_image_build_receives_explicit_source_identity() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    build_script = (DEPLOY / "build_push.sh").read_text(encoding="utf-8")

    assert "ARG LIBRAE_VERSION" in dockerfile
    assert "ARG LIBRAE_REVISION" in dockerfile
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LIBRAE=" in dockerfile
    assert 'org.opencontainers.image.version="${LIBRAE_VERSION}"' in dockerfile
    assert 'org.opencontainers.image.revision="${LIBRAE_REVISION}"' in dockerfile
    assert 'PLATFORMS="${TRADE_PLATFORMS:-linux/amd64,linux/arm64}"' in build_script
    assert 'docker buildx build --platform "${PLATFORMS}"' in build_script

    for script_name in ("build_push.sh", "trade.sh"):
        script = (DEPLOY / script_name).read_text(encoding="utf-8")
        assert '--build-arg LIBRAE_VERSION="' in script
        assert '--build-arg LIBRAE_REVISION="' in script
        assert "--build-arg UV_DEFAULT_INDEX" in script
        assert "--build-arg UV_INDEX" in script
        assert "--build-arg UV_FIND_LINKS" in script
        assert '--secret "id=uv_config,src=${' in script
        assert "UV_CONFIG_FILE not found:" in script
        assert "git -C " in script
        assert "rev-parse --verify HEAD" in script


def test_trade_image_workflow_builds_and_runs_the_real_image() -> None:
    workflow = (ROOT / ".github/workflows/trade-image.yml").read_text(encoding="utf-8")
    workflow_config = yaml.load(workflow, Loader=yaml.BaseLoader)

    assert workflow_config["on"]["push"]["branches"] == ["main"]
    assert "branches" not in workflow_config["on"]["pull_request"]
    assert workflow.count('- "deploy/**"') == 2
    assert "bash -n deploy/build_push.sh deploy/cloud_deploy.sh deploy/trade.sh" in workflow
    assert "if: github.event_name == 'push'" in workflow
    assert "docker/setup-qemu-action@v4" in workflow
    assert "docker/setup-buildx-action@v4" in workflow
    assert (
        "TRADE_PLATFORMS: ${{ github.event_name == 'pull_request' && "
        "'linux/amd64' || 'linux/amd64,linux/arm64' }}" in workflow
    )
    assert "registry:2.8.3" in workflow
    assert "docker:28.3.3-dind" in workflow
    assert "bash openssh rsync docker-cli-compose socat" in workflow
    assert "-o DisableForwarding=no" in workflow
    assert "-o AllowTcpForwarding=local" in workflow
    assert "./deploy/build_push.sh" in workflow
    assert "./deploy/cloud_deploy.sh deployment-target" in workflow
    assert "TRADE_IMAGE: localhost:5000/librae-trade" in workflow
    assert "UV_CONFIG_FILE: ${{ runner.temp }}/uv.toml" in workflow
    assert 'docker pull "${TRADE_IMAGE_REF}"' in workflow
    assert "docker image inspect" in workflow
    assert "docker run --rm" in workflow
    assert "EXPECTED_VERSION" in workflow
    assert 'Path("/app/uv.lock")' in workflow
    for distribution_name in ("numpy", "pandas", "ccxt", "shioaji", "ib-async"):
        assert f'"{distribution_name}"' in workflow
    assert "strategy-fixture/smoke/run.py" in workflow
    assert "TRADE_STRATEGY_PATH: ../strategy-fixture" in workflow
    assert "./deploy/trade.sh start smoke-ci ci USD smoke sim 60" in workflow
    assert "./deploy/trade.sh start smoke-registry ci USD smoke sim 1" in workflow
    assert "./deploy/trade.sh restart smoke-ci" in workflow
    assert "./deploy/trade.sh inspect failing-ci" in workflow
    assert "librae-account-lease-owner" in workflow
    assert 'io.librae.account_id" }}' in workflow
    assert "TRADE_TIMESCALE_DSN" in workflow
    assert "--network quant_network" in workflow
    assert "host.docker.internal:host-gateway" in workflow
    assert "test ! -e quant-deploy/.credentials" in workflow
    assert "test ! -e quant-deploy/strategies" in workflow
    assert "must-not-transfer" in workflow
    assert "Verify re-deploy preserves operator-owned files" in workflow
    assert "Remove disposable deployment infrastructure" in workflow
    assert "docker rm --force deployment-target deployment-registry" in workflow
    assert "docker network rm deployment-acceptance" in workflow


def test_registry_trade_deploy_uses_one_immutable_image_reference() -> None:
    public_env = (ROOT / ".env.example").read_text(encoding="utf-8")
    build_script = (DEPLOY / "build_push.sh").read_text(encoding="utf-8")
    trade_script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert "TRADE_IMAGE_REF=ghcr.io/<github-user>/quant-trade@sha256:" in public_env
    assert '--metadata-file "${METADATA_FILE}"' in build_script
    assert '"containerimage.digest"' in build_script
    assert 'echo "TRADE_IMAGE_REF=${IMAGE}@${DIGEST}"' in build_script
    assert "${IMAGE}:latest" not in build_script
    assert 'SOURCE_TAG="librae-${LIBRAE_REVISION:0:12}"' in build_script
    assert 'git -C "${STRATEGIES_DIR}"' not in build_script

    assert "@sha256:[0-9a-f]{64}" in trade_script
    assert 'docker pull -q "${image}"' in trade_script
    assert "${image}:latest" not in trade_script
    assert "TRADE_IMAGE is a publish repository, not a deployable reference." in trade_script
    assert 'image="quant-trade:local"' in trade_script
    assert 'org.opencontainers.image.revision" }}' in trade_script

    database_preflight = trade_script.index('echo "Checking strategy account and TimescaleDB')
    container_replacement = trade_script.index('docker rm "${container}"')
    assert trade_script.index('image="${TRADE_IMAGE_REF}"') < database_preflight
    assert database_preflight < container_replacement
    assert trade_script[database_preflight:].count('"${image}"') >= 2


def _run_trade_script(
    tmp_path: Path,
    *,
    image_reference: str,
    deployment_id: str = "smoke-main",
    account_id: str = "account-main",
    currency: str = "USD",
    strategy: str = "smoke",
    mode: str = "sim",
    config_file: Path | None = None,
    credentials_file: Path | None = None,
    fake_image_account_id: str | None = None,
    fake_image_currency: str | None = None,
    all_containers: str = "",
    running_containers: str = "",
    legacy_containers: str = "",
    existing_account_id: str = "",
    existing_strategy: str = "",
    existing_mode: str = "",
    existing_managed: str = "true",
    ready_token: str = "attempt-token",
    ready_marker: str = f"attempt-token:fixture-run:{'a' * 32}",
    ready_marker_exit_code: int = 0,
    status_after_ready_read: str = "",
    stale_ready_marker: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash = _find_bash()

    tmp_path.mkdir(parents=True, exist_ok=True)
    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${DOCKER_LOG}"
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
    if [[ "$*" == *"{{.Id}}"* ]]; then
        printf '%s\\n' "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    else
        printf '%s\\n' "<no value>"
    fi
elif [[ "$1" == "ps" && "${2:-}" == "-a" ]]; then
    printf '%s\\n' "${FAKE_DOCKER_ALL_CONTAINERS}"
elif [[ "$1" == "ps" && "$*" == *"name=quant_live_"* ]]; then
    printf '%s\\n' "${FAKE_DOCKER_LEGACY_CONTAINERS}"
elif [[ "$1" == "ps" ]]; then
    printf '%s\\n' "${FAKE_DOCKER_RUNNING_CONTAINERS}"
elif [[ "$1" == "inspect" ]]; then
    case "$*" in
        *io.librae.account_id*) printf '%s\\n' "${FAKE_EXISTING_ACCOUNT_ID}" ;;
        *io.librae.currency*) printf '%s\\n' "${FAKE_EXISTING_CURRENCY}" ;;
        *io.librae.strategy*) printf '%s\\n' "${FAKE_EXISTING_STRATEGY}" ;;
        *io.librae.mode*) printf '%s\\n' "${FAKE_EXISTING_MODE}" ;;
        *io.librae.managed*) printf '%s\\n' "${FAKE_EXISTING_MANAGED}" ;;
        *io.librae.ready_file*) printf '%s\\n' "${FAKE_READY_FILE}" ;;
        *io.librae.ready_token*) printf '%s\\n' "${FAKE_READY_TOKEN}" ;;
        *".State.Status}} {{.RestartCount"*)
            if [[ -s "${FAKE_DOCKER_STATE_FILE}" ]]; then
                printf '%s 0\\n' "$(cat "${FAKE_DOCKER_STATE_FILE}")"
            else
                printf '%s\\n' "running 0"
            fi
            ;;
        *State.Running*) printf '%s\\n' "false" ;;
        *State.Status*)
            if [[ -s "${FAKE_DOCKER_STATE_FILE}" ]]; then
                cat "${FAKE_DOCKER_STATE_FILE}"
            else
                printf '%s\\n' "running"
            fi
            ;;
        *RestartCount*) printf '%s\\n' "0" ;;
    esac
elif [[ "$1" == "run" && "$2" == "--rm" ]]; then
    expected_account_id=""
    expected_currency=""
    for argument in "$@"; do
        if [[ "${argument}" == TRADE_ACCOUNT_ID=* ]]; then
            expected_account_id="${argument#TRADE_ACCOUNT_ID=}"
        elif [[ "${argument}" == TRADE_CURRENCY=* ]]; then
            expected_currency="${argument#TRADE_CURRENCY=}"
        fi
    done
    if [[ "${expected_account_id}" != "${FAKE_IMAGE_ACCOUNT_ID}" ]]; then
        printf '%s\\n' \
            "strategy account_id mismatch: expected '${expected_account_id}', found '${FAKE_IMAGE_ACCOUNT_ID}'" \
            >&2
        exit 1
    fi
    if [[ "${expected_currency}" != "${FAKE_IMAGE_CURRENCY}" ]]; then
        printf '%s\\n' \
            "strategy currency mismatch: expected '${expected_currency}', found '${FAKE_IMAGE_CURRENCY}'" \
            >&2
        exit 1
    fi
elif [[ "$1" == "exec" ]]; then
    if [[ "$*" == *" cat "* ]]; then
        marker_file="${@: -1}"
        if [[ "${marker_file}" == "${FAKE_READY_FILE}" ]]; then
            [[ -z "${FAKE_READY_MARKER}" ]] || printf '%s\\n' "${FAKE_READY_MARKER}"
            if [[ -n "${FAKE_STATUS_AFTER_READY_READ}" ]]; then
                printf '%s\\n' "${FAKE_STATUS_AFTER_READY_READ}" \
                    > "${FAKE_DOCKER_STATE_FILE}"
            fi
        elif [[ "${marker_file}" == "/tmp/librae-ready" ]]; then
            [[ -z "${FAKE_STALE_READY_MARKER}" ]] || printf '%s\\n' "${FAKE_STALE_READY_MARKER}"
        fi
    fi
    exit "${FAKE_READY_MARKER_EXIT_CODE}"
fi
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_docker.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "DOCKER_LOG": docker_log.as_posix(),
            "FAKE_DOCKER_STATE_FILE": (tmp_path / "docker-state").as_posix(),
            "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
            "TRADE_IMAGE_REF": image_reference,
            "TRADE_TIMESCALE_DSN": ("postgresql://quant_app:secret@quant_timescaledb:5432/quant"),
            "TRADE_START_TIMEOUT_SECONDS": "2",
            "TRADE_STOP_TIMEOUT_SECONDS": "2",
            "FAKE_IMAGE_ACCOUNT_ID": fake_image_account_id or account_id,
            "FAKE_IMAGE_CURRENCY": fake_image_currency or currency,
            "FAKE_DOCKER_ALL_CONTAINERS": all_containers,
            "FAKE_DOCKER_RUNNING_CONTAINERS": running_containers,
            "FAKE_DOCKER_LEGACY_CONTAINERS": legacy_containers,
            "FAKE_EXISTING_ACCOUNT_ID": existing_account_id,
            "FAKE_EXISTING_CURRENCY": currency,
            "FAKE_EXISTING_STRATEGY": existing_strategy,
            "FAKE_EXISTING_MODE": existing_mode,
            "FAKE_EXISTING_MANAGED": existing_managed,
            "FAKE_READY_FILE": f"/tmp/librae-ready-{deployment_id}",
            "FAKE_READY_TOKEN": ready_token,
            "FAKE_READY_MARKER": ready_marker,
            "FAKE_READY_MARKER_EXIT_CODE": str(ready_marker_exit_code),
            "FAKE_STATUS_AFTER_READY_READ": status_after_ready_read,
            "FAKE_STALE_READY_MARKER": stale_ready_marker,
        }
    )
    command = [
        bash,
        str(DEPLOY / "trade.sh"),
        "start",
        deployment_id,
        account_id,
        currency,
        strategy,
        mode,
        "60",
    ]
    if config_file is not None:
        command.extend(("--config", str(config_file)))
    if credentials_file is not None:
        command.extend(("--credentials", str(credentials_file)))
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = docker_log.read_text(encoding="utf-8").splitlines() if docker_log.exists() else []
    return result, calls


def test_trade_script_rejects_mutable_image_before_docker(tmp_path: Path) -> None:
    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference="registry.example/librae-trade:latest",
    )

    assert result.returncode != 0
    assert "TRADE_IMAGE_REF must be digest-qualified" in result.stderr
    assert docker_calls == []


def test_trade_script_reuses_exact_digest_reference(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'a' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
    )

    assert result.returncode == 0, result.stderr
    assert any(call.startswith(f"pull -q {image_reference}") for call in docker_calls)
    assert any(call.startswith("run --rm ") and image_reference in call for call in docker_calls)
    assert any(call.startswith("run -d ") and image_reference in call for call in docker_calls)
    assert all(":latest" not in call for call in docker_calls)


def test_trade_script_does_not_accept_an_unrelated_ready_marker(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'3' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="not-ready",
        ready_marker="",
        stale_ready_marker="fixture-run:unrelated",
    )

    assert result.returncode != 0
    assert "did not become ready" in result.stderr
    assert any("LIBRAE_READY_FILE=/tmp/librae-ready-not-ready" in call for call in docker_calls)
    assert not any(call.endswith("cat /tmp/librae-ready") for call in docker_calls)


def test_trade_script_rejects_marker_from_failed_exec(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'4' * 64}"

    result, _ = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        ready_marker_exit_code=1,
    )

    assert result.returncode != 0
    assert "did not become ready" in result.stderr


def test_trade_script_rechecks_container_after_reading_marker(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'5' * 64}"

    result, _ = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        status_after_ready_read="exited",
    )

    assert result.returncode != 0
    assert "failed before becoming ready" in result.stderr


def test_trade_script_rejects_marker_for_another_attempt(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'6' * 64}"

    result, _ = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        ready_marker=f"other-attempt:fixture-run:{'b' * 32}",
    )

    assert result.returncode != 0
    assert "did not become ready" in result.stderr


def test_trade_script_rejects_strategy_account_mismatch_before_replacement(
    tmp_path: Path,
) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'b' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        account_id="account-main",
        fake_image_account_id="account-other",
        all_containers="quant_smoke-main",
    )

    assert result.returncode != 0
    assert "strategy account_id mismatch" in result.stderr
    assert not any(call.startswith("rm ") for call in docker_calls)
    assert not any(call.startswith("run -d ") for call in docker_calls)


def test_trade_script_rejects_strategy_currency_mismatch_before_replacement(
    tmp_path: Path,
) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'2' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        currency="USD",
        fake_image_currency="TWD",
        all_containers="quant_smoke-main",
    )

    assert result.returncode != 0
    assert "strategy currency mismatch" in result.stderr
    assert not any(call.startswith("rm ") for call in docker_calls)
    assert not any(call.startswith("run -d ") for call in docker_calls)


def test_trade_script_uses_account_specific_identity_config_and_credentials(
    tmp_path: Path,
) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'c' * 64}"
    config_file = tmp_path / "account-main.yaml"
    config_file.write_text(
        "strategy:\n  account:\n    account_id: account-main\n",
        encoding="utf-8",
    )
    credentials_file = tmp_path / "account-main.env"
    credentials_file.write_text(
        "IBKR_HOST=host.docker.internal\nIBKR_PORT=7497\nIBKR_CLIENT_ID=7\n",
        encoding="utf-8",
    )

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="momentum-main",
        account_id="account-main",
        strategy="momentum",
        mode="live",
        config_file=config_file,
        credentials_file=credentials_file,
    )

    assert result.returncode == 0, result.stderr
    final_run = next(call for call in docker_calls if call.startswith("run -d "))
    preflight = next(call for call in docker_calls if call.startswith("run --rm "))
    for call in (preflight, final_run):
        assert f"--env-file {credentials_file}" in call
        assert "TIMESCALE_DSN=postgresql://quant_app:secret@quant_timescaledb:5432/quant" in call
    assert "--name quant_momentum-main" in final_run
    assert "--label io.librae.deployment_id=momentum-main" in final_run
    assert "--label io.librae.account_id=account-main" in final_run
    assert "--label io.librae.currency=USD" in final_run
    assert "--label io.librae.strategy=momentum" in final_run
    assert "--label io.librae.mode=live" in final_run
    assert "--label io.librae.runtime_revision=sha256:" in final_run
    assert "--label io.librae.ready_file=/tmp/librae-ready-momentum-main" in final_run
    assert "LIBRAE_READY_FILE=/tmp/librae-ready-momentum-main" in final_run
    assert "--runtime-revision sha256:" in final_run
    assert "--config /app/deployment/config.yaml" in final_run
    assert "--add-host host.docker.internal:host-gateway" in final_run
    assert ".secrets:/app/.secrets:ro" not in final_run


def test_trade_script_accepts_optional_credentials_in_sim_mode(tmp_path: Path) -> None:
    """Brokers that require authentication even for read-only market data
    (e.g. ShioajiAdapter) have no other safe way to reach sim mode --
    --credentials must be optional in sim, not live-only. Uses the same
    --env-file mechanism as live mode, never sourced as shell code."""
    image_reference = f"registry.example/librae-trade@sha256:{'f' * 64}"
    credentials_file = tmp_path / "account-main.env"
    credentials_file.write_text(
        "IBKR_HOST=host.docker.internal\nIBKR_PORT=7497\nIBKR_CLIENT_ID=7\n",
        encoding="utf-8",
    )

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        mode="sim",
        credentials_file=credentials_file,
    )

    assert result.returncode == 0, result.stderr
    final_run = next(call for call in docker_calls if call.startswith("run -d "))
    assert f"--env-file {credentials_file}" in final_run
    assert "--label io.librae.mode=sim" in final_run


def test_trade_script_allows_same_strategy_for_independent_deployments(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'d' * 64}"

    first_result, first_calls = _run_trade_script(
        tmp_path / "first",
        image_reference=image_reference,
        deployment_id="momentum-main",
        account_id="account-main",
        strategy="momentum",
    )
    second_result, second_calls = _run_trade_script(
        tmp_path / "second",
        image_reference=image_reference,
        deployment_id="momentum-ira",
        account_id="account-ira",
        strategy="momentum",
    )

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert any("--name quant_momentum-main" in call for call in first_calls)
    assert any("--name quant_momentum-ira" in call for call in second_calls)


def test_trade_script_rejects_duplicate_running_live_account(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'e' * 64}"
    credentials_file = tmp_path / "account-main.env"
    credentials_file.write_text(
        "IBKR_HOST=host.docker.internal\n",
        encoding="utf-8",
    )

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="momentum-second",
        account_id="account-main",
        strategy="momentum",
        mode="live",
        credentials_file=credentials_file,
        running_containers="quant_momentum-first",
    )

    assert result.returncode != 0
    assert "already owned by live deployment quant_momentum-first" in result.stderr
    assert not any(call.startswith("rm ") for call in docker_calls)
    assert not any(call.startswith("run -d ") for call in docker_calls)


def test_trade_script_rejects_unlabeled_legacy_live_container(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'1' * 64}"
    credentials_file = tmp_path / "account-main.env"
    credentials_file.write_text(
        "IBKR_HOST=host.docker.internal\n",
        encoding="utf-8",
    )

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="momentum-main",
        account_id="account-main",
        strategy="momentum",
        mode="live",
        credentials_file=credentials_file,
        legacy_containers="quant_live_momentum",
        existing_managed="<no value>",
    )

    assert result.returncode != 0
    assert "legacy live container quant_live_momentum has no account binding" in result.stderr
    assert not any(call.startswith("rm ") for call in docker_calls)
    assert not any(call.startswith("run -d ") for call in docker_calls)


def test_trade_script_replaces_only_the_same_deployment_binding(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'f' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="momentum-main",
        account_id="account-main",
        strategy="momentum",
        all_containers="quant_momentum-main",
        existing_account_id="account-main",
        existing_strategy="momentum",
        existing_mode="sim",
    )

    assert result.returncode == 0, result.stderr
    assert docker_calls.count("rm quant_momentum-main") == 1


def test_trade_script_rejects_rebinding_an_existing_deployment(tmp_path: Path) -> None:
    image_reference = f"registry.example/librae-trade@sha256:{'0' * 64}"

    result, docker_calls = _run_trade_script(
        tmp_path,
        image_reference=image_reference,
        deployment_id="momentum-main",
        account_id="account-main",
        strategy="momentum",
        all_containers="quant_momentum-main",
        existing_account_id="account-other",
        existing_strategy="momentum",
        existing_mode="sim",
    )

    assert result.returncode != 0
    assert "already bound to account_id=account-other" in result.stderr
    assert not any(call.startswith("rm ") for call in docker_calls)
    assert not any(call.startswith("run -d ") for call in docker_calls)


def test_trade_container_uses_reachable_service_endpoints() -> None:
    public_env = (ROOT / ".env.example").read_text(encoding="utf-8")
    secrets_env = (ROOT / ".env.secrets.example").read_text(encoding="utf-8")
    script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert (
        "TIMESCALE_DSN=postgresql://quant_app:quant_app_secret@localhost:5432/quant" in public_env
    )
    assert (
        "TRADE_TIMESCALE_DSN=postgresql://quant_app:quant_app_secret@quant_timescaledb:5432/quant"
    ) in public_env
    assert "\nIBKR_HOST=\n" in secrets_env
    assert "IBKR_HOST=127.0.0.1" not in secrets_env
    assert "host.docker.internal" in secrets_env

    preflight = script.index('echo "Checking strategy account and TimescaleDB')
    replacement = script.index('docker rm "${container}"')
    assert preflight < replacement
    assert 'local trade_timescale_dsn="${TRADE_TIMESCALE_DSN:?' in script
    assert '-e TIMESCALE_DSN="${trade_timescale_dsn}"' in script
    assert '--add-host "host.docker.internal:host-gateway"' in script
    assert "IBKR_HOST cannot use container loopback" in script
    assert 'source "${PROJECT_ROOT}/.env.secrets"' not in script
    assert 'credential_args+=(--env-file "${credentials_file}")' in script
    assert '--filter "label=io.librae.managed=true"' in script
    assert "container_ready_file()" in script
    assert "container_ready_token()" in script
    assert "valid_ready_marker()" in script
    assert 'ready_file="${READY_FILE}"' in script
    assert script.count('container_ready_file "${container}"') == 3
    assert "cmd_inspect()" in script
    assert "cmd_restart()" in script
    assert 'stop_container "${container}" "${force}"' in script
    assert 'docker stop --signal TERM --time -1 "${container}"' in script
    assert 'docker kill --signal KILL "${container}"' in script
    assert '"${marker}" != "${previous_marker}"' in script


def test_remote_schema_path_matches_compose_source() -> None:
    script = (DEPLOY / "cloud_deploy.sh").read_text(encoding="utf-8")

    assert 'DEPLOY_TIMEOUT_SECONDS="${CLOUD_DEPLOY_TIMEOUT_SECONDS:-180}"' in script
    assert 'STAGE="file transfer"' in script
    assert 'STAGE="Compose startup"' in script
    assert 'STAGE="schema loading"' in script
    assert "Cloud deployment failed during ${STAGE}" in script
    assert 'rsync -az "${PROJECT_ROOT}/librae/db/timescale_init.sql"' in script
    assert '"${PROJECT_ROOT}/.env.secrets.example"' in script
    assert '"${PROJECT_ROOT}/.credentials"' not in script
    assert "${REMOTE_DIR}/librae/db/timescale_init.sql" in script
    assert "${REMOTE_DIR}/deploy/timescale_init.sql" not in script
    for variable in (
        "POSTGRES_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_GRAFANA_PASSWORD",
        "GF_SECURITY_ADMIN_PASSWORD",
    ):
        assert f"${{{variable}:?Set {variable} in .env}}" in script


def test_cloud_deploy_reports_the_failed_stage(tmp_path: Path) -> None:
    project = tmp_path / "project"
    script_dir = project / "deploy"
    script_dir.mkdir(parents=True)
    script = script_dir / "cloud_deploy.sh"
    shutil.copy2(DEPLOY / "cloud_deploy.sh", script)
    (project / ".env").write_text(
        "\n".join(
            (
                "POSTGRES_PASSWORD=test",
                "POSTGRES_APP_PASSWORD=test",
                "POSTGRES_GRAFANA_PASSWORD=test",
                "GF_SECURITY_ADMIN_PASSWORD=test",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text("#!/usr/bin/env bash\nexit 17\n", encoding="utf-8", newline="\n")
    fake_ssh.chmod(0o755)

    result = subprocess.run(
        [
            _find_bash(),
            "-c",
            'PATH="$PWD/fake-bin:$PATH"; exec deploy/cloud_deploy.sh deployment-target',
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 17
    assert "Cloud deployment failed during file transfer (exit 17)." in result.stderr


def test_dashboard_generator_does_not_import_the_engine_package() -> None:
    script = (ROOT / "scripts/dev_push_dashboard.py").read_text(encoding="utf-8")

    assert '"librae/app/grafana/generate_dashboards.py"' in script
    assert '"librae.app.grafana.generate_dashboards"' not in script


def test_grafana_receives_only_its_database_password() -> None:
    for compose_name in ("docker-compose.yml", "docker-compose.local.yml"):
        compose = yaml.safe_load((DEPLOY / compose_name).read_text(encoding="utf-8"))
        grafana = compose["services"]["grafana"]
        environment = grafana["environment"]

        assert "env_file" not in grafana
        assert "POSTGRES_GRAFANA_PASSWORD" in environment
        assert "POSTGRES_PASSWORD" not in environment
        assert "POSTGRES_APP_PASSWORD" not in environment


def test_reference_services_use_private_bindings_and_versioned_images() -> None:
    compose = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))
    local = yaml.safe_load((DEPLOY / "docker-compose.local.yml").read_text(encoding="utf-8"))

    assert compose["services"]["grafana"]["image"] == "grafana/grafana:13.1.1"
    assert local["services"]["grafana"]["image"] == "grafana/grafana:13.1.1"
    assert compose["services"]["timescaledb"]["image"] == "timescale/timescaledb:2.28.3-pg16"
    assert "127.0.0.1" in compose["services"]["grafana"]["ports"][0]
    assert "127.0.0.1" in local["services"]["grafana"]["ports"][0]


def test_database_roles_match_runtime_boundaries() -> None:
    schema = (ROOT / "librae/db/timescale_init.sql").read_text(encoding="utf-8")
    datasource = yaml.safe_load(
        (ROOT / "librae/app/grafana/provisioning/datasources/timescaledb.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert datasource["datasources"][0]["user"] == "grafana_reader"
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;" in schema
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quant_app;"
        in schema
    )
    assert "INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO grafana_reader" not in schema


def test_database_schema_does_not_embed_migrations() -> None:
    schema = (ROOT / "librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert r"\set ON_ERROR_STOP on" in schema
    assert "ALTER TABLE" not in schema
    assert "DROP INDEX" not in schema


def test_backtest_cache_identity_is_separate_from_config_hash() -> None:
    schema = (ROOT / "librae/db/timescale_init.sql").read_text(encoding="utf-8")

    assert "backtest_revision TEXT" in schema
    assert "backtest_cache_key VARCHAR(32)" in schema
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_backtest_runs_cache_key" in schema
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_backtest_runs_config_hash" not in schema


def test_local_trade_build_uses_explicit_strategy_context() -> None:
    script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert 'strategy_source="${TRADE_STRATEGY_PATH:-../strategies}"' in script
    assert '--build-context "strategy_source=${strategy_source}"' in script
    assert '"${strategy_source}/${strategy}/${required_file}"' in script
    assert '-f "${SCRIPT_DIR}/Dockerfile" "${PROJECT_ROOT}"' in script
