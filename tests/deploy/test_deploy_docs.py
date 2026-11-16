"""The deploy guide and everything that cites `deploy/` stay in step with it.

The reference deployment is documented prose plus shell, which no type checker
sees. These checks fail when a file, script or `trade.sh` command that a
document names stops existing, and when a file is added without being
documented.
"""

import re
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
GUIDE = ROOT / "docs/guides/optional-infrastructure.md"

# Historical by design (CONTRIBUTING.md, "Writing docs and comments").
HISTORICAL = ("docs/plans/", "docs/research/", "docs/spikes/", "docs/decisions/", "docs/learnings/")

DEPLOY_PATH = re.compile(r"(?:(?<=\./)|(?<![\w/-]))deploy/([A-Za-z0-9_][A-Za-z0-9_.-]*)")
BARE_SCRIPT = re.compile(r"(?<![\w./-])([a-z][a-z_]*\.sh)\b")
# An invocation (`./deploy/trade.sh start`, `trade.sh stop` in code), not prose
# such as "trade.sh injects".
TRADE_COMMAND = re.compile(r"(?:\./deploy/trade\.sh|`(?:deploy/)?trade\.sh) ([a-z]+)\b")


def _deploy_files() -> list[Path]:
    return sorted(path for path in DEPLOY.iterdir() if path.is_file())


def _cited_texts() -> Iterator[tuple[str, str]]:
    """Current-state text that may cite deploy files: docs, comments, templates."""
    paths = [
        *ROOT.glob("*.md"),
        *ROOT.glob(".env*.example"),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "librae").rglob("*.py"),
        *(ROOT / "scripts").glob("*.py"),
        *_deploy_files(),
    ]
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(HISTORICAL):
            continue
        yield relative, path.read_text(encoding="utf-8")


def test_every_deploy_file_is_in_the_guide() -> None:
    table = "\n".join(
        line for line in GUIDE.read_text(encoding="utf-8").splitlines() if line.startswith("|")
    )

    undocumented = [path.name for path in _deploy_files() if f"`{path.name}`" not in table]

    assert not undocumented, f"add to the deploy file table in {GUIDE.name}: {undocumented}"


def _workflow_and_test_texts() -> Iterator[tuple[str, str]]:
    paths = [*(ROOT / ".github/workflows").glob("*.yml"), *(ROOT / "tests").rglob("*.py")]
    for path in paths:
        if path != Path(__file__).resolve():
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def test_cited_deploy_files_exist() -> None:
    missing: list[str] = []
    for relative, text in [*_cited_texts(), *_workflow_and_test_texts()]:
        for name in DEPLOY_PATH.findall(text):
            name = name.rstrip(".")
            if not (DEPLOY / name).exists():
                missing.append(f"{relative}: deploy/{name}")

    assert not missing, missing


def test_cited_scripts_exist() -> None:
    scripts = {path.name for path in _deploy_files()}
    missing: list[str] = []
    for relative, text in _cited_texts():
        for name in BARE_SCRIPT.findall(text):
            if name not in scripts:
                missing.append(f"{relative}: {name}")

    assert not missing, missing


def test_cited_trade_commands_exist() -> None:
    header = (DEPLOY / "trade.sh").read_text(encoding="utf-8")
    commands = set(re.findall(r"^#   \./deploy/trade\.sh ([a-z]+)", header, re.MULTILINE))
    assert {"start", "inspect", "restart", "halt", "reset", "stop"} <= commands

    missing: list[str] = []
    for relative, text in _cited_texts():
        for command in TRADE_COMMAND.findall(text):
            if command not in commands:
                missing.append(f"{relative}: trade.sh {command}")

    assert not missing, missing
