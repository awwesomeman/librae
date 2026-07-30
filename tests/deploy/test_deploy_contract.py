"""Static checks for deployment files that cannot be exercised in unit tests."""

from pathlib import Path

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
    assert "COPY orchestration_helpers.py" not in dockerfile
    assert "**/.git" in dockerignore
    assert "**/.env.*" in dockerignore
    assert "**/.secrets" in dockerignore


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
    assert "docker build" in workflow
    assert "docker image inspect" in workflow
    assert "docker run --rm" in workflow
    assert "EXPECTED_VERSION" in workflow
    assert "strategies/smoke/run.py" in workflow
    assert "TRADE_TIMESCALE_DSN" in workflow
    assert "--network quant_network" in workflow
    assert "host.docker.internal:host-gateway" in workflow


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


def test_local_trade_build_uses_workspace_context_and_checks_strategies() -> None:
    script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert 'build_context="$(cd "${PROJECT_ROOT}/.." && pwd)"' in script
    assert '[[ ! -d "${build_context}/strategies" ]]' in script
    assert '"${SCRIPT_DIR}/.." >/dev/null' not in script
