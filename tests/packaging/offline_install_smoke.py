"""Install the built distribution from a local wheelhouse with indexes disabled."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLED_SMOKE = Path(__file__).with_name("installed_smoke.py")


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def main() -> None:
    wheels = list((ROOT / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one built wheel, found: {wheels}")

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_root = Path(temporary_directory)
        wheelhouse = temporary_root / "wheelhouse"
        environment = temporary_root / "environment"
        outside_checkout = temporary_root / "outside-checkout"
        wheelhouse.mkdir()
        outside_checkout.mkdir()

        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(wheelhouse),
                str(wheels[0]),
            ]
        )
        venv.EnvBuilder(with_pip=True).create(environment)

        offline_env = os.environ.copy()
        offline_env["PIP_NO_INDEX"] = "1"
        python = _venv_python(environment)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "librae",
            ],
            env=offline_env,
        )
        _run([str(python), str(INSTALLED_SMOKE)], cwd=outside_checkout, env=offline_env)


if __name__ == "__main__":
    main()
