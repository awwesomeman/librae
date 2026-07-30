"""Deployment contract checks that do not require a Docker daemon."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"


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

    assert '".[calendars,cli,db,crypto-live,telegram,tw-live,us-live]"' in dockerfile
    assert "COPY --from=strategy_source . strategies/" in dockerfile
    assert "COPY orchestration_helpers.py" not in dockerfile
    assert "**/.git" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "**/.secrets" in dockerignore
    assert "\ntests\n" in dockerignore
    assert "\ndocs\n" in dockerignore


def test_trade_image_build_receives_explicit_source_identity() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG LIBRAE_VERSION" in dockerfile
    assert "ARG LIBRAE_REVISION" in dockerfile
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_LIBRAE=" in dockerfile
    assert 'org.opencontainers.image.version="${LIBRAE_VERSION}"' in dockerfile
    assert 'org.opencontainers.image.revision="${LIBRAE_REVISION}"' in dockerfile

    for script_name in ("build_push.sh", "trade.sh"):
        script = (DEPLOY / script_name).read_text(encoding="utf-8")
        assert '--build-arg LIBRAE_VERSION="' in script
        assert '--build-arg LIBRAE_REVISION="' in script
        assert "git -C " in script
        assert "rev-parse --verify HEAD" in script


def test_trade_image_workflow_builds_and_runs_the_real_image() -> None:
    workflow = (ROOT / ".github/workflows/trade-image.yml").read_text(encoding="utf-8")

    assert workflow.count('- "deploy/**"') == 2
    assert "bash -n deploy/build_push.sh deploy/cloud_deploy.sh deploy/trade.sh" in workflow
    assert "docker build" in workflow
    assert "docker image inspect" in workflow
    assert "docker run --rm" in workflow
    assert "EXPECTED_VERSION" in workflow
    assert "strategy-fixture/smoke/run.py" in workflow
    assert "--build-context strategy_source=../strategy-fixture" in workflow
    assert "TRADE_TIMESCALE_DSN" in workflow
    assert "--network quant_network" in workflow
    assert "host.docker.internal:host-gateway" in workflow


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

    database_preflight = trade_script.index('echo "Checking TimescaleDB connectivity')
    container_replacement = trade_script.index('docker rm -f "${container}"')
    assert trade_script.index('image="${TRADE_IMAGE_REF}"') < database_preflight
    assert database_preflight < container_replacement
    assert trade_script[database_preflight:].count('"${image}"') >= 2


def _run_trade_script(
    tmp_path: Path,
    *,
    image_reference: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bash = shutil.which("bash")
    if bash is None and os.name == "nt":
        git_bash = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Git/bin/bash.exe"
        if git_bash.is_file():
            bash = str(git_bash)
    if bash is None:
        pytest.skip("bash is required for deployment script behavior tests")

    docker_log = tmp_path / "docker.log"
    fake_docker = tmp_path / "docker"
    fake_docker.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${DOCKER_LOG}"
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
    printf '%s\\n' "<no value>"
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
            "PATH": f"{tmp_path}{os.pathsep}{env['PATH']}",
            "TRADE_IMAGE_REF": image_reference,
            "TRADE_TIMESCALE_DSN": ("postgresql://quant_app:secret@quant_timescaledb:5432/quant"),
        }
    )
    result = subprocess.run(
        [bash, str(DEPLOY / "trade.sh"), "start", "smoke", "sim", "60"],
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

    preflight = script.index('echo "Checking TimescaleDB connectivity')
    replacement = script.index('docker rm -f "${container}"')
    assert preflight < replacement
    assert 'local trade_timescale_dsn="${TRADE_TIMESCALE_DSN:?' in script
    assert '-e TIMESCALE_DSN="${trade_timescale_dsn}"' in script
    assert '--add-host "host.docker.internal:host-gateway"' in script
    assert "IBKR_HOST cannot use container loopback" in script


def test_remote_schema_path_matches_compose_source() -> None:
    script = (DEPLOY / "cloud_deploy.sh").read_text(encoding="utf-8")

    assert 'rsync -az "${PROJECT_ROOT}/librae/db/timescale_init.sql"' in script
    assert "${REMOTE_DIR}/librae/db/timescale_init.sql" in script
    assert "${REMOTE_DIR}/deploy/timescale_init.sql" not in script
    for variable in (
        "POSTGRES_PASSWORD",
        "POSTGRES_APP_PASSWORD",
        "POSTGRES_GRAFANA_PASSWORD",
        "GF_SECURITY_ADMIN_PASSWORD",
    ):
        assert f"${{{variable}:?Set {variable} in .env}}" in script


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
