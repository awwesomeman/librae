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


def test_local_trade_build_uses_workspace_context_and_checks_strategies() -> None:
    script = (DEPLOY / "trade.sh").read_text(encoding="utf-8")

    assert 'build_context="$(cd "${PROJECT_ROOT}/.." && pwd)"' in script
    assert '[[ ! -d "${build_context}/strategies" ]]' in script
    assert '"${SCRIPT_DIR}/.." >/dev/null' not in script
