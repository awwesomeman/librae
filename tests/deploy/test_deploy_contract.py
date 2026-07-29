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

    assert (DEPLOY / source).resolve() == (ROOT / "db" / "timescale_init.sql").resolve()
    assert (DEPLOY / source).is_file()


def test_trade_image_installs_every_supported_runtime_extra() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (DEPLOY / "Dockerfile.dockerignore").read_text(encoding="utf-8")

    assert '".[db,crypto-live,tw-live,us-live]"' in dockerfile
    assert "COPY orchestration_helpers.py" not in dockerfile
    assert "**/.env.*" in dockerignore
    assert "**/.secrets" in dockerignore


def test_remote_schema_path_matches_compose_source() -> None:
    script = (DEPLOY / "cloud_deploy.sh").read_text(encoding="utf-8")

    assert 'rsync -az "${PROJECT_ROOT}/db/timescale_init.sql"' in script
    assert "${REMOTE_DIR}/db/timescale_init.sql" in script
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
    schema = (ROOT / "db" / "timescale_init.sql").read_text(encoding="utf-8")
    datasource = yaml.safe_load(
        (ROOT / "app/grafana/provisioning/datasources/timescaledb.yaml").read_text(encoding="utf-8")
    )

    assert datasource["datasources"][0]["user"] == "grafana_reader"
    assert "GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_reader;" in schema
    assert (
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO quant_app;"
        in schema
    )
    assert "INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO grafana_reader" not in schema


def test_local_trade_build_uses_workspace_context_and_checks_strategies() -> None:
    script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert 'build_context="$(cd "${PROJECT_ROOT}/.." && pwd)"' in script
    assert '[[ ! -d "${build_context}/strategies" ]]' in script
    assert '"${SCRIPT_DIR}/.." >/dev/null' not in script
